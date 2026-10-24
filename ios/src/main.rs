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
//! `clipboard` shares the phone's clipboard: each text copied on it as a line on standard output
//! ({"event":"clipboard","text"}), and {"op":"set","text"} on standard input put on it.
//!
//! `files` reaches the phone's media files (photos, Downloads, recordings: Apple's file service,
//! AFC): a command per line on standard input, each answered by a line on standard output.
//! {"op":"list","path"} → {"event":"list","path","entries":[{"name","folder","size","modified"}]};
//! {"op":"fetch","path","to"} → {"event":"fetched","path"} (to: a file here);
//! {"op":"send","from","path"} → {"event":"sent","path"} (from: a file here);
//! or {"event":"error","op","path","message"}.
//!
//!     handset-ios stream --pairing FILE --udid UDID [--address IP] [--port PORT] [--verbose]
//!     handset-ios apps --pairing FILE --udid UDID [--address IP] [--icons DIR] [--verbose]
//!     handset-ios clipboard --pairing FILE --udid UDID [--address IP] [--verbose]
//!     handset-ios files --pairing FILE --udid UDID [--address IP] [--verbose]

use std::error::Error;
use std::io::Write;
use std::net::IpAddr;
use std::time::{Duration, Instant};

use idevice::afc::AfcClient;
use idevice::afc::opcode::AfcFopenMode;
use idevice::core_device_proxy::CoreDeviceProxy;
use idevice::usbmuxd::{Connection, UsbmuxdAddr, UsbmuxdConnection};
use idevice::{IdeviceService, RsdService};
use idevice::core_device::{
    AppIconTarget, AppServiceClient, CallInfoBlob, DataInclusionPolicy, GENERAL_PASTEBOARD, IconServiceClient,
    PasteboardServiceClient, PasteboardSnapshot, DisplayServiceClient, HevcDepacketizer, RtpPacket, build_screen_audio_offer,
    build_start_audio_parameters, build_start_video_parameters,
};
use idevice::remote_pairing::{RemotePairingClient, RpPairingFile, RpPairingSocket, connect_tls_psk_tunnel_native};
use idevice::rsd::RsdHandshake;
use idevice::services::core_device::display_stream::negotiation::NEGOTIATOR_MODE_COREDEVICE_SCREEN;
use idevice::services::core_device::display_stream::{build_keyframe_request, build_liveness, is_rtcp};
use idevice::services::core_device::hid::{
    ButtonState, IndigoHidClient, TOUCHSCREEN_STATE_CONTACT, TOUCHSCREEN_STATE_RELEASE, UniversalHidServiceClient,
};
use futures::StreamExt;
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
/// A packet still missing once this many newer ones have come was lost (the picture comes over
/// UDP), not merely late.
const LOST_AFTER: u64 = 16;
/// While a keyframe is awaited, it's asked for again this often; one that doesn't come in time
/// gets the stream started again (which starts with one).
const KEYFRAME_AGAIN: Duration = Duration::from_millis(500);
const KEYFRAME_FOR: Duration = Duration::from_secs(3);

/// Which of the picture's packets were lost: each one's number seen, in whatever order they come.
#[derive(Default)]
struct Losses {
    next: Option<u16>,
    missing: std::collections::BTreeMap<u16, u64>, // a missing packet → how many had come then
    seen: u64,
}

impl Losses {
    /// A packet came: whether one before it is lost for good.
    fn saw(&mut self, number: u16) -> bool {
        self.seen += 1;
        let Some(next) = self.next else {
            self.next = Some(number.wrapping_add(1));
            return false;
        };
        let ahead = number.wrapping_sub(next);
        if ahead == 0 {
            self.next = Some(number.wrapping_add(1));
        } else if ahead < 0x8000 {
            if ahead > 1000 {
                // A jump this big is a stream started again, not packets lost.
                self.missing.clear();
            } else {
                for gap in 0..ahead {
                    self.missing.insert(next.wrapping_add(gap), self.seen);
                }
            }
            self.next = Some(number.wrapping_add(1));
        } else {
            self.missing.remove(&number); // late, not lost
        }
        let before = self.missing.len();
        let seen = self.seen;
        self.missing.retain(|_, when| seen - *when < LOST_AFTER);
        self.missing.len() < before
    }
}

/// Where a keyframe starts in Annex-B bytes (at its parameter sets, or its first slice).
fn keyframe_start(bytes: &[u8]) -> Option<usize> {
    let mut at = 0;
    while at + 3 < bytes.len() {
        if bytes[at] == 0 && bytes[at + 1] == 0 && bytes[at + 2] == 1 {
            let kind = (bytes[at + 3] >> 1) & 0x3f;
            if (16..=23).contains(&kind) || (32..=34).contains(&kind) {
                return Some(if at > 0 && bytes[at - 1] == 0 { at - 1 } else { at });
            }
            at += 3;
        } else {
            at += 1;
        }
    }
    None
}

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
    if !["stream", "apps", "clipboard", "files"].contains(&command.as_str()) {
        return Err("usage: handset-ios stream|apps|clipboard|files --pairing FILE --udid UDID [--address IP] [--port PORT]".into());
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
    let verbose = options.verbose;
    let (way, (mut adapter, mut handshake)) = reach(&options).await?;
    if verbose {
        eprintln!("reached {way}");
    }
    let mut apps = AppServiceClient::connect_rsd(&mut adapter, &mut handshake).await?;
    // As a stream: asked for its list in one reply, the phone (iOS 27) never answers.
    let streamed = tokio::time::timeout(APPS_FOR, async {
        let mut found = Vec::new();
        let mut stream = Box::pin(apps.stream_apps(false, true, false, false, true));
        while let Some(app) = stream.next().await {
            found.push(app?);
        }
        Ok::<_, idevice::IdeviceError>(found)
    })
    .await
    .map_err(|_| "The iPhone didn't list its apps in time.")??;
    // The apps the home screen has: every app that can be removed (the person's, and most of
    // Apple's), and the two of Apple's that can't; not the system's helpers and extensions.
    let mut listed: Vec<_> = streamed
        .into_iter()
        .filter(|app| !app.is_hidden && !app.is_app_clip)
        .filter(|app| app.is_removable || HOME_SCREEN_BUILT_INS.contains(&app.bundle_identifier.as_str()))
        .collect();
    listed.sort_by_key(|app| app.name.to_lowercase());
    if verbose {
        eprintln!("{} apps listed", listed.len());
    }
    // Icons are worth a little wait each, not the list: one that doesn't come is left out, and
    // after a few of those, the rest are too.
    let mut icons = match &options.icons {
        Some(_) => match tokio::time::timeout(ICON_FOR * 2, IconServiceClient::connect_rsd(&mut adapter, &mut handshake)).await {
            Ok(Ok(client)) => Some(client),
            other => {
                if verbose {
                    eprintln!("no icon service: {}", if other.is_err() { "it didn't answer".to_string() } else { "refused".to_string() });
                }
                None
            }
        },
        None => None,
    };
    let mut misses = 0;
    for app in listed {
        let mut icon = Value::Null;
        if let (Some(client), Some(folder)) = (icons.as_mut(), &options.icons) {
            let target = AppIconTarget::BundleIdentifier(app.bundle_identifier.clone());
            match tokio::time::timeout(ICON_FOR, client.fetch_icon(target, 60.0, 60.0, 3.0, false)).await {
                Ok(Ok(fetched)) if !fetched.is_placeholder => {
                    let path = std::path::Path::new(folder).join(format!("{}.png", app.bundle_identifier));
                    let png: Vec<u8> = fetched.png_data.into();
                    if std::fs::write(&path, png).is_ok() {
                        icon = json!(path.to_string_lossy());
                    }
                }
                Ok(Ok(_)) => {}
                Ok(Err(why)) if verbose => eprintln!("no icon for {}: {why}", app.bundle_identifier),
                Ok(Err(_)) => {}
                Err(_) => {
                    misses += 1;
                    if verbose {
                        eprintln!("no icon in time for {}", app.bundle_identifier);
                    }
                }
            }
        }
        if misses >= 3 {
            icons = None; // the icon service has stopped answering: the list goes on without
        }
        println!("{}", json!({"name": app.name, "bundle": app.bundle_identifier, "icon": icon,
                              "removable": app.is_removable, "first_party": app.is_first_party,
                              "internal": app.is_internal, "path": app.path}));
    }
    Ok(())
}

/// Apple's apps on the home screen that can't be removed (the others that can't are the
/// system's own helpers: stickers, message extensions, previews).
const HOME_SCREEN_BUILT_INS: [&str; 2] = ["com.apple.mobilephone", "com.apple.Preferences"];
/// How long the phone may take to list its apps, and to give each one's icon.
const APPS_FOR: Duration = Duration::from_secs(30);
const ICON_FOR: Duration = Duration::from_secs(5);
/// Text up to this size comes with a look at the clipboard; bigger, it's left on the phone.
const CLIPBOARD_TEXT: i64 = 256 * 1024;
/// How often the clipboard's change count is looked at, besides the phone's own word of changes.
const CLIPBOARD_LOOK: Duration = Duration::from_secs(3);

/// The phone's clipboard, both ways: the phone says when it changes (when it does: it's looked at
/// every few seconds too, by its change count alone), and what's set here goes on it. What it
/// holds when this starts isn't news; nor is what was just set from here.
async fn clipboard(options: Options) -> Fallible<()> {
    let (_way, (mut adapter, mut handshake)) = reach(&options).await?;
    let mut watcher = PasteboardServiceClient::connect_rsd(&mut adapter, &mut handshake).await?;
    let mut board = PasteboardServiceClient::connect_rsd(&mut adapter, &mut handshake).await?;
    let first = board.get_with_policy(GENERAL_PASTEBOARD, DataInclusionPolicy::AllPromised).await?;
    let (mut count, mut last): (Option<i64>, Option<String>) = (first.change_count, None);
    let (pushed, mut pushes) = mpsc::channel::<PasteboardSnapshot>(8);
    if watcher
        .set_change_notifications(true, GENERAL_PASTEBOARD, Some(DataInclusionPolicy::Threshold(CLIPBOARD_TEXT)))
        .await
        .is_ok()
    {
        tokio::spawn(async move {
            while let Ok(snapshot) = watcher.recv_push().await {
                if pushed.send(snapshot).await.is_err() {
                    break;
                }
            }
        });
    }
    let (sent, mut commands) = mpsc::channel::<Value>(16);
    tokio::spawn(async move {
        let mut lines = tokio::io::BufReader::new(tokio::io::stdin()).lines();
        while let Ok(Some(line)) = lines.next_line().await {
            if let Ok(command) = serde_json::from_str::<Value>(&line) {
                if sent.send(command).await.is_err() {
                    break;
                }
            }
        }
    });
    say(json!({"event": "clipboard", "ready": true}));
    let mut look = tokio::time::interval(CLIPBOARD_LOOK);
    let mut listening = true;
    loop {
        tokio::select! {
            snapshot = pushes.recv(), if listening => match snapshot {
                Some(snapshot) => news(snapshot, &mut count, &mut last),
                None => listening = false,
            },
            command = commands.recv() => {
                let Some(command) = command else { break };
                if command.get("op").and_then(Value::as_str) == Some("set") {
                    let text = command.get("text").and_then(Value::as_str).unwrap_or("").to_string();
                    if !text.is_empty() && last.as_deref() != Some(text.as_str()) {
                        board.set_text(&text, GENERAL_PASTEBOARD).await?;
                        last = Some(text);
                        count = board.get_with_policy(GENERAL_PASTEBOARD, DataInclusionPolicy::AllPromised).await?.change_count;
                    }
                }
            }
            _ = look.tick() => {
                let now = board.get_with_policy(GENERAL_PASTEBOARD, DataInclusionPolicy::AllPromised).await?;
                if now.change_count.is_some() && now.change_count != count {
                    let snapshot = board.get_with_policy(GENERAL_PASTEBOARD, DataInclusionPolicy::Threshold(CLIPBOARD_TEXT)).await?;
                    news(snapshot, &mut count, &mut last);
                }
            }
        }
    }
    Ok(())
}

/// The phone's media files, a command at a time (the module's head says which).
async fn files(options: Options) -> Fallible<()> {
    let (_way, (mut adapter, mut handshake)) = reach(&options).await?;
    let mut afc = AfcClient::connect_rsd(&mut adapter, &mut handshake).await?;
    say(json!({"event": "files", "ready": true}));
    let mut lines = tokio::io::BufReader::new(tokio::io::stdin()).lines();
    while let Ok(Some(line)) = lines.next_line().await {
        let Ok(command) = serde_json::from_str::<Value>(&line) else { continue };
        let field = |name: &str| command.get(name).and_then(Value::as_str).unwrap_or("").to_string();
        let (op, path) = (field("op"), field("path"));
        let answer = match op.as_str() {
            "list" => list_folder(&mut afc, &path).await,
            "fetch" => fetch_file(&mut afc, &path, &field("to")).await,
            "send" => send_file(&mut afc, &field("from"), &path).await,
            _ => continue,
        };
        let answer = answer.unwrap_or_else(|why| json!({"event": "error", "op": op, "path": path, "message": why.to_string()}));
        println!("{answer}");
    }
    Ok(())
}

async fn list_folder(afc: &mut AfcClient, path: &str) -> Fallible<Value> {
    let mut entries = Vec::new();
    for name in afc.list_dir(path).await? {
        if name == "." || name == ".." {
            continue;
        }
        let full = format!("{}/{}", path.trim_end_matches('/'), name);
        if let Ok(info) = afc.get_file_info(&full).await {
            entries.push(json!({
                "name": name,
                "folder": info.st_ifmt == "S_IFDIR",
                "size": info.size,
                "modified": info.modified.and_utc().timestamp(),
            }));
        }
    }
    Ok(json!({"event": "list", "path": path, "entries": entries}))
}

async fn fetch_file(afc: &mut AfcClient, path: &str, to: &str) -> Fallible<Value> {
    let mut file = afc.open(path, AfcFopenMode::RdOnly).await?;
    let mut out = tokio::fs::File::create(to).await?;
    tokio::io::copy(&mut file, &mut out).await?;
    file.close().await?;
    Ok(json!({"event": "fetched", "path": path, "to": to}))
}

async fn send_file(afc: &mut AfcClient, from: &str, path: &str) -> Fallible<Value> {
    let mut input = tokio::fs::File::open(from).await?;
    let mut file = afc.open(path, AfcFopenMode::WrOnly).await?;
    tokio::io::copy(&mut input, &mut file).await?;
    tokio::io::AsyncWriteExt::flush(&mut file).await?;
    file.close().await?;
    Ok(json!({"event": "sent", "path": path}))
}

/// A clipboard snapshot: its text said, if it's new.
fn news(snapshot: PasteboardSnapshot, count: &mut Option<i64>, last: &mut Option<String>) {
    if snapshot.change_count.is_some() && snapshot.change_count == *count {
        return;
    }
    *count = snapshot.change_count;
    if let Some(text) = snapshot.text() {
        if last.as_deref() != Some(text.as_str()) {
            println!("{}", json!({"event": "clipboard", "text": text}));
            *last = Some(text);
        }
    }
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
    // someone's watching. A packet lost breaks the picture until the next keyframe (the frames
    // after it are changes to it): one is asked for at once, and the picture waits for it
    // rather than show the broken ones (green, with what changes drawn over it).
    let mut frames = HevcDepacketizer::new();
    // Handset reads the picture at its own pace: it's written from a thread of its own, so a slow
    // read never holds up the packets coming in (the phone's link drops those not taken in time,
    // and each one lost costs a keyframe).
    let (to_handset, units) = std::sync::mpsc::channel::<Vec<u8>>();
    std::thread::spawn(move || {
        let mut out = std::io::BufWriter::new(std::io::stdout().lock());
        for unit in units {
            if out.write_all(&unit).and_then(|_| out.flush()).is_err() {
                break; // Handset stopped reading
            }
        }
    });
    let (mut media_ssrc, mut fir) = (0u32, 0u8);
    let mut alive = tokio::time::interval(Duration::from_secs(1));
    let mut heard = Instant::now();
    let mut losses = Losses::default();
    let mut awaiting: Option<Instant> = None; // since when a keyframe is awaited, after a loss
    let mut asked = Instant::now(); // when one was last asked for
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
                let lost = losses.saw(rtp.sequence_number);
                let mut ask = media_ssrc == 0
                    || (awaiting.is_some() && asked.elapsed() >= KEYFRAME_AGAIN);
                if lost && awaiting.is_none() {
                    if options.verbose {
                        eprintln!("a picture packet was lost: asking for a keyframe");
                    }
                    frames = HevcDepacketizer::new(); // not waiting on what won't come
                    awaiting = Some(Instant::now());
                    ask = true;
                }
                if media_ssrc == 0 {
                    media_ssrc = rtp.ssrc;
                }
                if ask {
                    let request = build_keyframe_request(our_ssrc, "handset@desk", media_ssrc, &[], fir);
                    fir = fir.wrapping_add(1);
                    asked = Instant::now();
                    let _ = video.send_to(datagram.source_port, request).await;
                }
                frames.push(rtp.sequence_number, rtp.timestamp, rtp.payload);
                let unit = frames.take_output();
                let unit = match awaiting {
                    None => &unit[..],
                    Some(_) => match keyframe_start(&unit) {
                        Some(at) => {
                            awaiting = None;
                            &unit[at..]
                        }
                        None => &[][..],
                    },
                };
                if !unit.is_empty() && to_handset.send(unit.to_vec()).is_err() {
                    break; // Handset stopped reading
                }
            }
            _ = alive.tick() => {
                if media_ssrc != 0 {
                    let _ = video.send_to(VIDEO_PORT, build_liveness(our_ssrc, "handset@desk", &[])).await;
                }
                let unanswered = awaiting.is_some_and(|since| since.elapsed() > KEYFRAME_FOR);
                if heard.elapsed() > QUIET || unanswered {
                    if options.verbose {
                        eprintln!("the phone's stream went quiet: starting it again");
                    }
                    heard = Instant::now();
                    media_ssrc = 0; // and a keyframe is asked for again
                    losses = Losses::default();
                    // The new stream's first frames are a keyframe's: shown from there.
                    frames = HevcDepacketizer::new();
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
    let offer = screen_offer(&call_id(), call, &screen_blob(our_ssrc))?;
    let answer = display
        .start_media_stream(build_start_video_parameters(ours, video_port, theirs, VIDEO_PORT, offer, 140, 1, session))
        .await?;
    // What the phone settled on (its bit rate, frame rate, size), for Handset's debugging log.
    let settled = answer.as_dictionary().and_then(|a| a.get("connection")).and_then(|c| c.as_dictionary())
        .and_then(|c| c.get("streamConfig"));
    if let Some(config) = settled {
        eprintln!("the phone's stream: {config:?}");
    }
    Ok(())
}

/// A protobuf message, written field by field (as the crate's own encoder writes them, zeros
/// too).
#[derive(Default)]
struct Message(Vec<u8>);

impl Message {
    fn varint(&mut self, mut value: u64) {
        while value >= 0x80 {
            self.0.push(value as u8 | 0x80);
            value >>= 7;
        }
        self.0.push(value as u8);
    }
    fn number(&mut self, field: u64, value: u64) {
        self.varint(field << 3);
        self.varint(value);
    }
    fn bytes(&mut self, field: u64, value: &[u8]) {
        self.varint(field << 3 | 2);
        self.varint(value.len() as u64);
        self.0.extend_from_slice(value);
    }
    fn message(&mut self, field: u64, fill: impl FnOnce(&mut Message)) {
        let mut inner = Message::default();
        fill(&mut inner);
        self.bytes(field, &inner.0);
    }
}

/// The screen's media offer (AVConference's VCMediaNegotiationBlob), as the idevice crate makes
/// it (Xcode's), but for two settings: feedback allowed, so the phone honours the keyframes the
/// picture asks for after a lost packet (it ignores them otherwise); and no long-term reference
/// frames, which leave the picture torn after a loss. The phone keeps the rest as Xcode's: its own
/// screen size and frame rate, and at most 6 Mbit/s however much more is offered.
fn screen_blob(ssrc: u32) -> Vec<u8> {
    let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap_or_default();
    let ntp = ((now.as_secs() + 2_208_988_800) << 32) | ((u64::from(now.subsec_nanos()) << 32) / 1_000_000_000);
    media_blob(ssrc, ntp, true, false, b"FLS;SW:1;")
}

/// The blob, its settings given: `feedback` (allowRTCPFB), `ltrp` (long-term reference
/// frames) and the HEVC codec's feature string.
fn media_blob(ssrc: u32, ntp: u64, feedback: bool, ltrp: bool, hevc_features: &[u8]) -> Vec<u8> {
    fn formats(codec: &mut Message, variants: &[u64]) {
        for &variant in variants {
            codec.message(2, |format| {
                format.number(1, 1);
                format.number(2, variant);
                format.number(3, 0xc3c3);
                format.number(4, 0);
            });
        }
    }
    let mut blob = Message::default();
    blob.number(1, 1);
    blob.number(2, 1);
    blob.message(5, |screen| {
        screen.number(1, u64::from(ssrc));
        screen.number(2, u64::from(feedback)); // allowRTCPFB
        screen.message(3, |codec| {
            codec.number(1, 123); // H.264
            formats(codec, &[1, 2, 1, 2]);
            codec.bytes(3, b"FLS;SW:1;");
            codec.number(4, 1);
        });
        screen.message(3, |codec| {
            codec.number(1, 100); // HEVC
            formats(codec, &[1, 2]);
            codec.bytes(3, hevc_features);
            codec.number(4, 14);
        });
        screen.number(7, u64::from(ltrp)); // ltrpEnabled
        screen.number(8, 63);
        screen.number(12, 1);
    });
    blob.bytes(6, b"Viceroy 1.7.0");
    blob.number(8, 0);
    for (id, value, flags) in [(0, 20_000_000, Some(0x18000)), (4074, 0, Some(0x4000)), (0, 60_000_000, Some(0x40000)),
        (0, 40_000_000, Some(0x3000)), (16, 4100, None), (0, 6_000_000, Some(0x20000)), (4, 6500, None),
        (0, 100_000_000, Some(0x100000)), (1, 299, None), (0, 75_000_000, Some(0x80000))]
    {
        blob.message(9, |param| {
            param.number(1, id);
            param.number(2, value);
            if let Some(flags) = flags {
                param.number(3, flags);
            }
        });
    }
    blob.number(13, ntp);
    blob.number(14, 2);
    blob.number(16, 0);
    blob.number(18, 1);
    blob.0
}

/// The offer as the phone takes it: a binary plist with the blob zlib-compressed.
fn screen_offer(call_id: &str, call: &CallInfoBlob, blob: &[u8]) -> Fallible<Vec<u8>> {
    let mut compressed = flate2::write::ZlibEncoder::new(Vec::new(), flate2::Compression::default());
    compressed.write_all(blob)?;
    let mut offer = plist::Dictionary::new();
    offer.insert("avcMediaStreamOptionCallID".into(), plist::Value::String(call_id.into()));
    offer.insert("avcMediaStreamNegotiatorMediaBlob".into(), plist::Value::Data(compressed.finish()?));
    offer.insert("avcMediaStreamNegotiatorMode".into(), plist::Value::Integer(NEGOTIATOR_MODE_COREDEVICE_SCREEN.into()));
    offer.insert("avcMediaStreamOptionRemoteEndpointInfo".into(), plist::Value::Data(call.encode()));
    let mut out = Vec::new();
    plist::to_writer_binary(&mut out, &plist::Value::Dictionary(offer)).map_err(|e| e.to_string())?;
    Ok(out)
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
    let done = match options.command.as_str() {
        "apps" => list_apps(options).await,
        "clipboard" => clipboard(options).await,
        "files" => files(options).await,
        _ => stream(options).await,
    };
    if let Err(why) = done {
        say(json!({"event": "error", "message": why.to_string()}));
        std::process::exit(1);
    }
    // Out now: the runtime would otherwise wait for its read of standard input, which only ends
    // when Handset closes it (not when the helper is told to stop).
    std::process::exit(0);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn late_packets_are_not_lost() {
        let mut losses = Losses::default();
        assert!(!losses.saw(10));
        assert!(!losses.saw(12)); // 11 is late
        assert!(!losses.saw(11));
        for number in 13..40 {
            assert!(!losses.saw(number));
        }
    }

    #[test]
    fn a_packet_that_never_comes_is_lost_once() {
        let mut losses = Losses::default();
        assert!(!losses.saw(65534));
        let lost: Vec<bool> = (0..20u16).map(|i| losses.saw(65535u16.wrapping_add(1 + i))).collect();
        // 65535 never came: lost once enough newer ones have come (across the wrap to 0).
        assert_eq!(lost.iter().filter(|l| **l).count(), 1);
    }

    #[test]
    fn a_stream_started_again_is_not_a_loss() {
        let mut losses = Losses::default();
        losses.saw(100);
        assert!(!losses.saw(30000));
        for number in 30001..30040 {
            assert!(!losses.saw(number));
        }
    }

    #[test]
    fn the_offer_is_xcodes_but_for_its_two_settings() {
        use idevice::services::core_device::display_stream::negotiation::{SCREEN_HEVC_FLS, build_screen_media_blob};
        let (ssrc, ntp) = (0x1234_5678, 0xE9C0_0000_8000_0000);
        assert_eq!(media_blob(ssrc, ntp, false, true, SCREEN_HEVC_FLS.as_bytes()), build_screen_media_blob(ssrc, ntp));
        assert_ne!(media_blob(ssrc, ntp, true, false, b"FLS;SW:1;"), build_screen_media_blob(ssrc, ntp));
    }

    #[test]
    fn keyframes_found() {
        let slice = [0, 0, 0, 1, 0x02, 0x01, 0xaa]; // a trailing picture's slice (type 1)
        let vps = [0, 0, 0, 1, 0x40, 0x01, 0xbb]; // parameter sets (type 32)
        let idr = [0, 0, 1, 0x26, 0x01, 0xcc]; // a keyframe's slice (type 19)
        assert_eq!(keyframe_start(&slice), None);
        assert_eq!(keyframe_start(&[&slice[..], &vps[..]].concat()), Some(slice.len()));
        assert_eq!(keyframe_start(&idr), Some(0));
    }
}
