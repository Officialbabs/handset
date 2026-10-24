//! Handset's iPhone helper: it reaches the iPhone the ways Ada's phone service does, by its cable
//! and over Wi-Fi at once, whichever answers first (a tunnel in user space to CoreDevice's
//! services: by cable through usbmuxd and CoreDeviceProxy, over Wi-Fi with Apple's remote pairing
//! and the pairing Ada's desktop already made), and then:
//!
//! - writes the phone's screen to standard output, as H.265 (Annex B) from the phone's own
//!   screen-sharing stream;
//! - reads commands from standard input, one JSON object per line: {"op":"down"|"move","x","y"}
//!   with x and y from 0 to 65535 across the screen, {"op":"up"}, {"op":"button","name"} (home,
//!   lock, volume_up, volume_down), {"op":"key","name"} and {"op":"text","text"};
//! - says what it's doing on standard error, one JSON object per line: {"event":"status"},
//!   {"event":"video","codec":"h265"}, {"event":"input","ready":true}, {"event":"error"}.
//!
//! The end of standard input stops the phone's stream and ends the helper. {"op":"launch",
//! "bundle"} opens an app on the phone (said back as {"event":"launch","ok"}).
//!
//! `apps` lists the phone's apps instead, one JSON object per line on standard output
//! ({"name","bundle","icon"}), with each icon saved as a PNG in the --icons folder.
//!
//!     handset-ios stream --pairing FILE --udid UDID [--address IP] [--port PORT] [--verbose]
//!     handset-ios apps --pairing FILE --udid UDID [--address IP] [--icons DIR] [--verbose]

use std::error::Error;
use std::io::Write;
use std::net::IpAddr;
use std::time::{Duration, Instant};

use idevice::core_device_proxy::CoreDeviceProxy;
use idevice::usbmuxd::{Connection, UsbmuxdAddr, UsbmuxdConnection};
use idevice::{IdeviceService, RsdService};
use idevice::core_device::{
    AppIconTarget, AppServiceClient, CallInfoBlob, IconServiceClient, DisplayServiceClient, HevcDepacketizer, RtpPacket, build_screen_audio_offer,
    build_screen_video_offer, build_start_audio_parameters, build_start_video_parameters,
};
use idevice::remote_pairing::{RemotePairingClient, RpPairingFile, RpPairingSocket, connect_tls_psk_tunnel_native};
use idevice::rsd::RsdHandshake;
use idevice::services::core_device::display_stream::{build_keyframe_request, build_liveness, is_rtcp};
use idevice::services::core_device::hid::{
    ButtonState, IndigoHidClient, TOUCHSCREEN_STATE_CONTACT, TOUCHSCREEN_STATE_RELEASE, UniversalHidServiceClient,
};
use idevice::tcp::adapter::Adapter;
use serde_json::{Value, json};
use tokio::io::AsyncBufReadExt;
use tokio::net::TcpStream;
use tokio::sync::mpsc;

type Fallible<T> = Result<T, Box<dyn Error + Send + Sync>>;

/// The phone sends its screen from this port, and its sound (needed to open the session) from
/// the one before.
const VIDEO_PORT: u16 = 50001;
const AUDIO_PORT: u16 = 50000;
/// The phone takes touches a moment after its screen starts being sent.
const INPUT_AFTER: Duration = Duration::from_millis(400);
/// How long to look for the phone on the network.
const BROWSE_FOR: Duration = Duration::from_secs(12);
/// How long reaching the phone may take, once found: a sleeping phone doesn't answer at all,
/// and TCP alone would wait minutes to say so.
const REACH_FOR: Duration = Duration::from_secs(20);
/// Where iPhones' remote pairing listens, when looking it up finds nothing (as Ada's desktop
/// has always found it).
const PAIRING_PORT: u16 = 49152;
/// The phone's stream going this quiet means someone stopped it: the phone has one screen
/// stream for everyone, and stopping it stops them all (Ada's desktop does when it gets the phone
/// ready for her). It's started again.
const QUIET: Duration = Duration::from_secs(5);

/// One line on standard error, for Handset.
fn say(event: Value) {
    eprintln!("{event}");
}

fn status(text: &str) {
    say(json!({"event": "status", "text": text}));
}

struct Options {
    command: String,
    icons: Option<String>,
    pairing: String,
    udid: String,
    address: Option<IpAddr>,
    port: Option<u16>,
    verbose: bool,
}

fn options() -> Result<Options, String> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let command = args.first().cloned().unwrap_or_default();
    if command != "stream" && command != "apps" {
        return Err("usage: handset-ios stream|apps --pairing FILE --udid UDID [--address IP] [--port PORT]".into());
    }
    let value = |name: &str| args.iter().position(|a| a == name).and_then(|i| args.get(i + 1)).cloned();
    Ok(Options {
        command,
        icons: value("--icons"),
        pairing: value("--pairing").ok_or("--pairing FILE is needed")?,
        udid: value("--udid").ok_or("--udid UDID is needed")?,
        address: value("--address").map(|a| a.parse().map_err(|_| format!("{a} isn't an address"))).transpose()?,
        port: value("--port").map(|p| p.parse().map_err(|_| format!("{p} isn't a port"))).transpose()?,
        verbose: args.iter().any(|a| a == "--verbose"),
    })
}

/// The phone's remote-pairing service on the network: its address and port. Phones advertise it
/// as _remotepairing._tcp; the port changes, so it's looked up every time.
fn browse(address: Option<IpAddr>, verbose: bool) -> Fallible<(IpAddr, u16)> {
    let daemon = mdns_sd::ServiceDaemon::new()?;
    let events = daemon.browse("_remotepairing._tcp.local.")?;
    let until = Instant::now() + BROWSE_FOR;
    let mut found: Vec<(IpAddr, u16)> = Vec::new();
    while let Some(left) = until.checked_duration_since(Instant::now()) {
        let Ok(event) = events.recv_timeout(left) else { break };
        if let mdns_sd::ServiceEvent::ServiceResolved(info) = event {
            let port = info.get_port();
            for ip in info.get_addresses() {
                let ip: IpAddr = ip.to_ip_addr();
                if verbose {
                    eprintln!("found {} at {ip}:{port}", info.get_fullname());
                }
                if address.is_none_or(|wanted| wanted == ip) {
                    found.push((ip, port));
                }
            }
            // The phone Ada knows by its address: that's it. Without one, take a moment for
            // others to answer, so two phones are never mixed up.
            if address.is_some() && !found.is_empty() {
                break;
            }
        }
    }
    let _ = daemon.shutdown();
    found.sort();
    found.dedup();
    match found.as_slice() {
        [one] => Ok(*one),
        [] => Err("The iPhone isn't plugged in or on this Wi-Fi: plug it in, or unlock it and keep it near.".into()),
        _ => Err("More than one iPhone is here: Handset needs the address Ada knows.".into()),
    }
}

/// A key on a US keyboard, as a HID usage: (usage, with Shift).
fn usage(c: char) -> Option<(u64, bool)> {
    const SHIFTED: &str = "!@#$%^&*()";
    Some(match c {
        'a'..='z' => (0x04 + (c as u64 - 'a' as u64), false),
        'A'..='Z' => (0x04 + (c as u64 - 'A' as u64), true),
        '1'..='9' => (0x1E + (c as u64 - '1' as u64), false),
        '0' => (0x27, false),
        '\n' => (0x28, false),
        '\t' => (0x2B, false),
        ' ' => (0x2C, false),
        '-' => (0x2D, false),
        '_' => (0x2D, true),
        '=' => (0x2E, false),
        '+' => (0x2E, true),
        '[' => (0x2F, false),
        '{' => (0x2F, true),
        ']' => (0x30, false),
        '}' => (0x30, true),
        '\\' => (0x31, false),
        '|' => (0x31, true),
        ';' => (0x33, false),
        ':' => (0x33, true),
        '\'' => (0x34, false),
        '"' => (0x34, true),
        '`' => (0x35, false),
        '~' => (0x35, true),
        ',' => (0x36, false),
        '<' => (0x36, true),
        '.' => (0x37, false),
        '>' => (0x37, true),
        '/' => (0x38, false),
        '?' => (0x38, true),
        _ => {
            let shifted = SHIFTED.find(c)?;
            // ! is Shift+1 … ( is Shift+9, ) is Shift+0.
            let digit = if shifted == 9 { 0x27 } else { 0x1E + shifted as u64 };
            (digit, true)
        }
    })
}

/// A named key, as a HID usage on the keyboard page.
fn key_usage(name: &str) -> Option<u64> {
    Some(match name {
        "enter" => 0x28,
        "escape" => 0x29,
        "delete" => 0x2A,
        "tab" => 0x2B,
        "forward_delete" => 0x4C,
        "right" => 0x4F,
        "left" => 0x50,
        "down" => 0x51,
        "up" => 0x52,
        _ => return None,
    })
}

/// A button, as (usage on the consumer page, how long it's held).
fn button_usage(name: &str) -> Option<(u64, u64)> {
    Some(match name {
        "home" => (0x40, 80),
        "lock" => (0x30, 200),
        "volume_up" => (0xE9, 80),
        "volume_down" => (0xEA, 80),
        _ => return None,
    })
}

/// Commands from Handset, carried out on the phone in order.
async fn work_input(
    mut touch: UniversalHidServiceClient<Box<dyn idevice::ReadWrite>>,
    mut keys: IndigoHidClient<Box<dyn idevice::ReadWrite>>,
    mut apps: Option<AppServiceClient<Box<dyn idevice::ReadWrite>>>,
    mut commands: mpsc::Receiver<Value>,
) {
    let (mut last_x, mut last_y) = (0u16, 0u16);
    let at = |v: &Value, k: &str| v.get(k).and_then(Value::as_u64).unwrap_or(0).min(65535) as u16;
    while let Some(command) = commands.recv().await {
        let result: Result<(), idevice::IdeviceError> = async {
            match command.get("op").and_then(Value::as_str).unwrap_or("") {
                "down" | "move" => {
                    (last_x, last_y) = (at(&command, "x"), at(&command, "y"));
                    touch.send_touchscreen(TOUCHSCREEN_STATE_CONTACT, last_x, last_y, None).await?;
                }
                "up" => touch.send_touchscreen(TOUCHSCREEN_STATE_RELEASE, last_x, last_y, None).await?,
                "button" => {
                    if let Some((code, hold)) = command.get("name").and_then(Value::as_str).and_then(button_usage) {
                        keys.send_button(0x0C, code, ButtonState::Down).await?;
                        tokio::time::sleep(Duration::from_millis(hold)).await;
                        keys.send_button(0x0C, code, ButtonState::Up).await?;
                    }
                }
                "key" => {
                    if let Some(code) = command.get("name").and_then(Value::as_str).and_then(key_usage) {
                        keys.send_keyboard(code, ButtonState::Down).await?;
                        keys.send_keyboard(code, ButtonState::Up).await?;
                    }
                }
                "text" => {
                    let text = command.get("text").and_then(Value::as_str).unwrap_or("");
                    for (code, shift) in text.chars().filter_map(usage) {
                        if shift {
                            keys.send_keyboard(0xE1, ButtonState::Down).await?;
                        }
                        keys.send_keyboard(code, ButtonState::Down).await?;
                        keys.send_keyboard(code, ButtonState::Up).await?;
                        if shift {
                            keys.send_keyboard(0xE1, ButtonState::Up).await?;
                        }
                        tokio::time::sleep(Duration::from_millis(12)).await;
                    }
                }
                "launch" => {
                    // In front, as it was if it was open: FrontBoard brings it forward.
                    let bundle = command.get("bundle").and_then(Value::as_str).unwrap_or("").to_string();
                    match apps.as_mut() {
                        Some(apps) => match apps.launch_application(bundle.clone(), &[], false, false, None, None, None).await {
                            Ok(_) => say(json!({"event": "launch", "ok": true, "bundle": bundle})),
                            Err(why) => say(json!({"event": "launch", "ok": false, "bundle": bundle, "message": why.to_string()})),
                        },
                        None => say(json!({"event": "launch", "ok": false, "bundle": bundle,
                                           "message": "The iPhone didn't let Handset open apps."})),
                    }
                }
                _ => {}
            }
            Ok(())
        }
        .await;
        if let Err(error) = result {
            eprintln!("input: {error}");
        }
    }
}

/// The phone's developer services, reached: a tunnel to them with its own TCP in user space, and
/// their handshake.
type Reached = (idevice::tcp::handle::AdapterHandle, RsdHandshake);

/// How a way to the phone missed it: the phone isn't there that way at all (no cable), or it is
/// and something stopped it, in words.
enum Missed {
    Absent,
    Failed(String),
}

/// The phone's apps that show on its home screen, a line each, with their icons saved to the
/// --icons folder as BUNDLE.png.
async fn list_apps(options: Options) -> Fallible<()> {
    let (_way, (mut adapter, mut handshake)) = reach(&options).await?;
    let mut apps = AppServiceClient::connect_rsd(&mut adapter, &mut handshake).await?;
    let mut listed: Vec<_> = apps
        .list_apps(false, true, false, false, true)
        .await?
        .into_iter()
        .filter(|app| !app.is_hidden && !app.is_app_clip)
        .collect();
    listed.sort_by_key(|app| app.name.to_lowercase());
    let mut icons = match &options.icons {
        Some(_) => IconServiceClient::connect_rsd(&mut adapter, &mut handshake).await.ok(),
        None => None,
    };
    for app in listed {
        let mut icon = Value::Null;
        if let (Some(client), Some(folder)) = (icons.as_mut(), &options.icons) {
            let target = AppIconTarget::BundleIdentifier(app.bundle_identifier.clone());
            match client.fetch_icon(target, 60.0, 60.0, 3.0, false).await {
                Ok(fetched) if !fetched.is_placeholder => {
                    let path = std::path::Path::new(folder).join(format!("{}.png", app.bundle_identifier));
                    let png: Vec<u8> = fetched.png_data.into();
                    if std::fs::write(&path, png).is_ok() {
                        icon = json!(path.to_string_lossy());
                    }
                }
                Ok(_) => {}
                Err(why) if options.verbose => eprintln!("no icon for {}: {why}", app.bundle_identifier),
                Err(_) => {}
            }
        }
        println!("{}", json!({"name": app.name, "bundle": app.bundle_identifier, "icon": icon}));
    }
    Ok(())
}

async fn stream(options: Options) -> Fallible<()> {
    status("Looking for the iPhone on its cable and on Wi-Fi");
    let (way, (mut adapter, mut handshake)) = reach(&options).await?;
    if options.verbose {
        eprintln!("phone {} reached {way}", options.udid);
    }
    stream_screen(options, way, &mut adapter, &mut handshake).await
}

/// The phone both ways at once, by its cable and over Wi-Fi: the first way to reach its services
/// is used and the other given up, the cable first when both are there together. Neither
/// reaching it, the cable's reason counts when the phone is plugged in, Wi-Fi's otherwise.
async fn reach(options: &Options) -> Fallible<(&'static str, Reached)> {
    let cable = tokio::time::timeout(REACH_FOR, by_cable(&options.udid));
    let wifi = by_wifi(options);
    tokio::pin!(cable, wifi);
    let (mut cable_missed, mut wifi_missed): (Option<Missed>, Option<String>) = (None, None);
    loop {
        tokio::select! {
            biased;
            result = &mut cable, if cable_missed.is_none() => match result {
                Ok(Ok(reached)) => return Ok(("by cable", reached)),
                Ok(Err(missed)) => cable_missed = Some(missed),
                Err(_) => cable_missed = Some(Missed::Failed("The iPhone is plugged in but didn't answer: unlock it.".into())),
            },
            result = &mut wifi, if wifi_missed.is_none() => match result {
                Ok(reached) => return Ok(("over Wi-Fi", reached)),
                Err(why) => wifi_missed = Some(why.to_string()),
            },
        }
        if let (Some(cable), Some(wifi)) = (&cable_missed, &wifi_missed) {
            return Err(match cable {
                Missed::Failed(why) => why.clone(),
                Missed::Absent => wifi.clone(),
            }
            .into());
        }
    }
}

/// The phone by its cable: usbmuxd lists it while it's plugged in, and the pairing the phone keeps
/// from trusting this computer opens CoreDeviceProxy, a tunnel to the same services as over
/// Wi-Fi (as Ada's phone service does by cable). Nothing here asks the phone to trust anyone.
async fn by_cable(udid: &str) -> Result<Reached, Missed> {
    let plugged = async {
        let mut usbmuxd = UsbmuxdConnection::default().await.ok()?;
        usbmuxd.get_device(udid).await.ok().filter(|device| device.connection_type == Connection::Usb)
    };
    let device = plugged.await.ok_or(Missed::Absent)?;
    let failed = |why: String| Missed::Failed(format!("The iPhone is plugged in but its cable link didn't open ({why}): unlock it."));
    let proxy = CoreDeviceProxy::connect(&device.to_provider(UsbmuxdAddr::default(), "handset"))
        .await
        .map_err(|e| failed(e.to_string()))?;
    let rsd_port = proxy.tunnel_info().server_rsd_port;
    let mut adapter = proxy.create_software_tunnel().map_err(|e| failed(e.to_string()))?.to_async_handle();
    let services = adapter.connect(rsd_port).await.map_err(|e| failed(e.to_string()))?;
    let handshake = RsdHandshake::new(services).await.map_err(|e| failed(e.to_string()))?;
    Ok((adapter, handshake))
}

/// The phone over Wi-Fi: found on the network (or where Ada's service last saw it), then reached
/// with the pairing Ada's desktop made. Its reasons are only ever shown when the phone isn't
/// plugged in either.
async fn by_wifi(options: &Options) -> Fallible<Reached> {
    let (ip, port) = match (options.address, options.port) {
        (Some(ip), Some(port)) => (ip, port),
        (address, _) => {
            let verbose = options.verbose;
            match tokio::task::spawn_blocking(move || browse(address, verbose)).await? {
                Ok(found) => found,
                Err(_) if address.is_some() => (address.unwrap(), PAIRING_PORT),
                Err(why) => return Err(why),
            }
        }
    };
    if options.verbose {
        eprintln!("phone {} at {ip}:{port}", options.udid);
    }
    tokio::time::timeout(REACH_FOR, over_wifi(ip, port, &options.pairing))
        .await
        .map_err(|_| "The iPhone went to sleep or left Wi-Fi: it didn't answer.")?
}

/// The phone's developer services over Wi-Fi: the pairing Ada's desktop made, checked again
/// (never made anew here: that needs the person at the phone, and Ada's own card), then a tunnel
/// with its own TCP in user space, and the services' handshake.
async fn over_wifi(ip: IpAddr, port: u16, pairing: &str) -> Fallible<Reached> {
    let mut pairing = RpPairingFile::read_from_file(pairing).await?;
    let unreachable = "The iPhone went to sleep or left Wi-Fi: it didn't answer.";
    let socket = RpPairingSocket::new(TcpStream::connect((ip, port)).await.map_err(|_| unreachable)?);
    let mut remote = RemotePairingClient::new(socket, "handset");
    remote.connect(&mut pairing, || async { String::new() }).await.map_err(|e| -> Box<dyn Error + Send + Sync> {
        // A connection that drops or never answers is the phone away, not the pairing refused;
        // only a refusal of the pairing itself needs Ada to set the phone up again.
        let why = e.to_string();
        if why.contains("socket") || why.contains("io") || why.contains("timed out") || why.contains("closed") {
            unreachable.into()
        } else {
            format!("The iPhone didn't accept this computer ({why}): set it up with Ada again.").into()
        }
    })?;

    // A tunnel to the phone's developer services, with its own TCP in user space.
    let tunnel_port = remote.create_tcp_listener().await?;
    let tunnel = connect_tls_psk_tunnel_native(TcpStream::connect((ip, tunnel_port)).await?, remote.encryption_key()).await?;
    let host: IpAddr = tunnel.info.client_address.parse()?;
    let peer: IpAddr = tunnel.info.server_address.parse()?;
    let rsd_port = tunnel.info.server_rsd_port;
    let mut adapter = Adapter::new(Box::new(tunnel.into_inner()), host, peer).to_async_handle();
    let handshake = RsdHandshake::new(adapter.connect(rsd_port).await?).await?;
    Ok((adapter, handshake))
}

async fn stream_screen(
    options: Options,
    way: &str,
    adapter: &mut idevice::tcp::handle::AdapterHandle,
    handshake: &mut RsdHandshake,
) -> Fallible<()> {

    // The phone's screen, as its screen-sharing stream: sound first (it opens the session),
    // then the picture, in one session.
    status(&format!("Starting the iPhone's screen {way}"));
    let mut display = DisplayServiceClient::connect_rsd(&mut *adapter, &mut *handshake)
        .await
        .map_err(|e| format!("The iPhone's screen sharing isn't there ({e}): turn on Developer Mode."))?;
    let audio = adapter.bind_udp(0).await?;
    let video = adapter.bind_udp(0).await?;
    let (ours, theirs) = (adapter.host_ip().to_string(), adapter.peer_ip().to_string());
    let call = CallInfoBlob {
        call_id: 0,
        client_version: 1,
        device_type: "Mac17,7".into(),
        framework_version: "2205.3.1".into(),
        os_version: "25F71".into(),
        device_name: None,
        audio_device_uid: None,
    };
    let our_ssrc = uuid::Uuid::new_v4().as_u128() as u32;
    let (audio_port, video_port) = (audio.local_port(), video.local_port());
    start_screen(&mut display, &call, (&ours, audio_port, video_port), &theirs, our_ssrc).await?;
    say(json!({"event": "video", "codec": "h265"}));

    // Touches and keys, once the phone takes them.
    tokio::time::sleep(INPUT_AFTER).await;
    let (send, commands) = mpsc::channel::<Value>(256);
    match (
        UniversalHidServiceClient::connect_rsd(&mut *adapter, &mut *handshake).await,
        IndigoHidClient::connect_rsd(&mut *adapter, &mut *handshake).await,
    ) {
        (Ok(touch), Ok(keys)) => {
            let apps = AppServiceClient::connect_rsd(&mut *adapter, &mut *handshake).await.ok();
            tokio::spawn(work_input(touch, keys, apps, commands));
            say(json!({"event": "input", "ready": true}));
        }
        (touch, keys) => {
            let why = touch.err().or(keys.err()).map(|e| e.to_string()).unwrap_or_default();
            eprintln!("no input: {why}");
            say(json!({"event": "input", "ready": false}));
        }
    }

    // Handset's commands, until it closes standard input.
    let (closed_send, mut closed) = mpsc::channel::<()>(1);
    tokio::spawn(async move {
        let mut lines = tokio::io::BufReader::new(tokio::io::stdin()).lines();
        while let Ok(Some(line)) = lines.next_line().await {
            if let Ok(command) = serde_json::from_str::<Value>(&line) {
                let _ = send.send(command).await;
            }
        }
        let _ = closed_send.send(()).await;
    });

    // The picture: RTP from the phone, its frames out as they come. A keyframe is asked for
    // when the first packet comes, so the decoder can start, and the phone hears regularly that
    // someone's watching.
    let mut frames = HevcDepacketizer::new();
    let mut out = std::io::BufWriter::new(std::io::stdout().lock());
    let (mut media_ssrc, mut fir) = (0u32, 0u8);
    let mut alive = tokio::time::interval(Duration::from_secs(1));
    let mut heard = Instant::now();
    let mut ended = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
    loop {
        tokio::select! {
            packet = video.recv() => {
                let datagram = packet?;
                heard = Instant::now();
                if is_rtcp(&datagram.data) {
                    continue;
                }
                let Some(rtp) = RtpPacket::parse(&datagram.data) else { continue };
                if media_ssrc == 0 {
                    media_ssrc = rtp.ssrc;
                    let request = build_keyframe_request(our_ssrc, "handset@desk", media_ssrc, &[], fir);
                    fir = fir.wrapping_add(1);
                    let _ = video.send_to(datagram.source_port, request).await;
                }
                frames.push(rtp.sequence_number, rtp.timestamp, rtp.payload);
                let unit = frames.take_output();
                if !unit.is_empty() && out.write_all(&unit).and_then(|_| out.flush()).is_err() {
                    break; // Handset stopped reading
                }
            }
            _ = alive.tick() => {
                if media_ssrc != 0 {
                    let _ = video.send_to(VIDEO_PORT, build_liveness(our_ssrc, "handset@desk", &[])).await;
                }
                if heard.elapsed() > QUIET {
                    if options.verbose {
                        eprintln!("the phone's stream went quiet: starting it again");
                    }
                    heard = Instant::now();
                    media_ssrc = 0; // and a keyframe is asked for again
                    if let Err(why) = start_screen(&mut display, &call, (&ours, audio_port, video_port), &theirs, our_ssrc).await {
                        // The connection itself is gone (the phone was unplugged, slept, or left
                        // Wi-Fi): Handset starts over, with a new one, whichever way is there.
                        eprintln!("starting the stream again: {why}");
                        return Err(format!("The iPhone's link {way} went: it was unplugged, went to sleep or left Wi-Fi.").into());
                    }
                }
            }
            _ = closed.recv() => break,
            _ = ended.recv() => break,
        }
    }
    // No stop_media_stream: it stops every stream the phone has (stopAll), Ada's too, and her
    // touches with it. Closing the tunnel ends this one.
    Ok(())
}

/// Starts the phone's screen stream to our two ports: sound first (it opens the session), then
/// the picture, in one session.
async fn start_screen(
    display: &mut DisplayServiceClient<Box<dyn idevice::ReadWrite>>,
    call: &CallInfoBlob,
    (ours, audio_port, video_port): (&str, u16, u16),
    theirs: &str,
    our_ssrc: u32,
) -> Fallible<()> {
    let session = uuid::Uuid::new_v4();
    let call_id = || uuid::Uuid::new_v4().to_string().to_uppercase();
    let offer = build_screen_audio_offer(&call_id(), call)?;
    display
        .start_media_stream(build_start_audio_parameters(ours, audio_port, theirs, AUDIO_PORT, offer, 140, session))
        .await?;
    let offer = build_screen_video_offer(&call_id(), call, our_ssrc)?;
    display
        .start_media_stream(build_start_video_parameters(ours, video_port, theirs, VIDEO_PORT, offer, 140, 1, session))
        .await?;
    Ok(())
}

#[tokio::main]
async fn main() {
    let options = match options() {
        Ok(options) => options,
        Err(why) => {
            say(json!({"event": "error", "message": why}));
            std::process::exit(2);
        }
    };
    let done = if options.command == "apps" { list_apps(options).await } else { stream(options).await };
    if let Err(why) = done {
        say(json!({"event": "error", "message": why.to_string()}));
        std::process::exit(1);
    }
}
