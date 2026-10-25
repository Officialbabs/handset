//! Setting an iPhone up by its cable, a step at a time, where no phone service on the desktop
//! has (`handset-ios setup STEP --udid UDID`):
//!
//!     trust           this computer trusted: the phone asks (Trust, then its passcode), and the
//!                     pairing goes to usbmuxd, where every program looking for it finds it
//!     developer-mode  Developer Mode on: its switch shown in Settings, then waited for (the phone
//!                     restarts, and asks again once unlocked)
//!     ddi --images F  Apple's developer disk image (Xcode's, in folder F) signed by Apple's server
//!                     for this phone, and installed: the screen's touch service lives in it. A
//!                     restart takes it away; `stream --images F` puts it back.
//!     wifi --out F    paired for Wi-Fi (Apple's remote pairing), into file F
//!
//! It says how it's going on standard error, one JSON object per line: {"event":"status"|
//! "waiting"|"done", "message"}; "waiting" is the person's turn, on the phone.

use std::time::{Duration, Instant};

use idevice::amfi::AmfiClient;
use idevice::cryptexd::{self, Cryptex1Assets};
use idevice::lockdown::LockdownClient;
use idevice::remote_pairing::{RemotePairingLockdownService, RpPairingFile};
use idevice::rsd::RsdHandshake;
use idevice::usbmuxd::{Connection, UsbmuxdAddr, UsbmuxdConnection, UsbmuxdDevice};
use idevice::{IdeviceError, IdeviceService};
use serde_json::json;

use crate::{Fallible, Missed, by_cable, say};

/// How long the person has for each thing asked of them on the phone.
const TRUST_FOR: Duration = Duration::from_secs(180);
const DEVELOPER_MODE_FOR: Duration = Duration::from_secs(600);
const PLUG_IN_FOR: Duration = Duration::from_secs(120);
const LOOK_AGAIN: Duration = Duration::from_secs(2);

fn tell(kind: &str, message: &str) {
    say(json!({"event": kind, "message": message}));
}

pub async fn setup(step: &str, udid: &str, images: Option<&str>, out: Option<&str>) -> Fallible<()> {
    match step {
        "trust" => trust(udid).await,
        "developer-mode" => developer_mode(udid).await,
        "ddi" => ddi(udid, images.ok_or("--images FOLDER is needed")?).await,
        "wifi" => wifi(udid, out.ok_or("--out FILE is needed")?).await,
        _ => Err(format!("no step {step}: trust, developer-mode, ddi or wifi").into()),
    }
}

/// The phone on its cable, as usbmuxd lists it, waited for (it may be restarting).
async fn plugged(udid: &str, wait: Duration) -> Fallible<UsbmuxdDevice> {
    let until = Instant::now() + wait;
    let mut told = false;
    loop {
        if let Ok(mut usbmuxd) = UsbmuxdConnection::default().await {
            if let Ok(device) = usbmuxd.get_device(udid).await {
                if device.connection_type == Connection::Usb {
                    return Ok(device);
                }
            }
        }
        if Instant::now() > until {
            return Err("The iPhone isn't plugged in: plug it in by its cable, and set it up again.".into());
        }
        if !told {
            tell("waiting", "Plug the iPhone in by its cable, and unlock it");
            told = true;
        }
        tokio::time::sleep(LOOK_AGAIN).await;
    }
}

fn host_name() -> String {
    std::fs::read_to_string("/proc/sys/kernel/hostname")
        .map(|name| name.trim().to_string())
        .ok()
        .filter(|name| !name.is_empty())
        .unwrap_or_else(|| "Linux".into())
}

/// This computer's own id (its machine id), so its pairing is its own and not every computer's
/// with the same name.
fn machine_id() -> String {
    std::fs::read_to_string("/etc/machine-id").map(|id| id.trim().to_string()).unwrap_or_else(|_| host_name())
}

async fn trust(udid: &str) -> Fallible<()> {
    let device = plugged(udid, PLUG_IN_FOR).await?;
    let provider = device.to_provider(UsbmuxdAddr::default(), "handset");
    let mut usbmuxd = UsbmuxdConnection::default().await?;
    if let Ok(record) = usbmuxd.get_pair_record(udid).await {
        let mut lockdown = LockdownClient::connect(&provider).await?;
        if lockdown.start_session(&record).await.is_ok() {
            tell("done", "This computer is trusted");
            return Ok(());
        }
    }
    let buid = usbmuxd.get_buid().await?;
    let host_id = uuid::Uuid::new_v4().to_string().to_uppercase();
    let name = host_name();
    let until = Instant::now() + TRUST_FOR;
    loop {
        let left = until.checked_duration_since(Instant::now()).ok_or(
            "The iPhone wasn't trusted in time: unplug it and plug it in again to be asked again.",
        )?;
        let mut lockdown = LockdownClient::connect(&provider).await?;
        tell("waiting", "Tap Trust on the iPhone, then enter its passcode");
        match tokio::time::timeout(left, lockdown.pair(&host_id, &buid, Some(&name))).await {
            Err(_) => return Err("The iPhone wasn't trusted in time: unplug it and plug it in again to be asked again.".into()),
            Ok(Ok(record)) => {
                usbmuxd.save_pair_record(udid, record.clone().serialize()?).await?;
                let mut lockdown = LockdownClient::connect(&provider).await?;
                lockdown.start_session(&record).await?;
                tell("done", "This computer is trusted");
                return Ok(());
            }
            Ok(Err(IdeviceError::PasswordProtected)) => {
                tell("waiting", "Unlock the iPhone");
                tokio::time::sleep(LOOK_AGAIN).await;
            }
            Ok(Err(IdeviceError::UserDeniedPairing)) => {
                return Err("The iPhone said Don't Trust: unplug it and plug it in again to be asked again.".into());
            }
            Ok(Err(other)) => return Err(format!("The iPhone didn't pair ({other}).").into()),
        }
    }
}

async fn developer_mode(udid: &str) -> Fallible<()> {
    let until = Instant::now() + DEVELOPER_MODE_FOR;
    let mut shown = false;
    loop {
        // Plugged in, unlocked once since it started: AMFI answers; restarting, it isn't there.
        let device = plugged(udid, until.saturating_duration_since(Instant::now())).await?;
        let provider = device.to_provider(UsbmuxdAddr::default(), "handset");
        if let Ok(mut amfi) = AmfiClient::connect(&provider).await {
            match amfi.get_developer_mode_status().await {
                Ok(true) => {
                    tell("done", "Developer Mode is on");
                    return Ok(());
                }
                Ok(false) if !shown => {
                    // Its switch only shows in Settings once a computer asks.
                    let _ = amfi.reveal_developer_mode_option_in_ui().await;
                    tell(
                        "waiting",
                        "On the iPhone: Settings › Privacy & Security › Developer Mode, turn it on and \
                         restart. Then unlock it, tap Turn On, and enter its passcode",
                    );
                    shown = true;
                }
                _ => {}
            }
        }
        if Instant::now() > until {
            return Err("Developer Mode isn't on yet: turn it on, then set the iPhone up again.".into());
        }
        tokio::time::sleep(LOOK_AGAIN).await;
    }
}

/// Xcode's developer disk image in `folder`, as the phone's cryptex service takes it.
pub async fn assets(folder: &str) -> Fallible<Cryptex1Assets> {
    let read = |name: &str| tokio::fs::read(std::path::Path::new(folder).join(name));
    let manifest: plist::Dictionary = plist::from_bytes(&read("BuildManifest.plist").await?)?;
    let identity = idevice::tss::select_cryptex_build_identity(&manifest)?.clone();
    Ok(Cryptex1Assets::from_parts(
        read("Image.dmg").await?,
        read("Image.dmg.trustcache").await?,
        read("Image.dmg.cryptex_info").await?,
        read("Image.dmg.root_hash").await?,
        identity,
    ))
}

/// The developer disk image on the phone: there already, or installed now (signed for it by
/// Apple's server, so this needs the internet). True when it was installed now.
pub async fn ensure_ddi(
    adapter: &mut idevice::tcp::handle::AdapterHandle,
    handshake: &mut RsdHandshake,
    folder: &str,
) -> Fallible<bool> {
    if cryptexd::installed_ddi(&mut *adapter, &mut *handshake).await?.is_some() {
        return Ok(false);
    }
    let assets = assets(folder).await?;
    cryptexd::install_ddi(&mut *adapter, &mut *handshake, &assets).await.map_err(|e| match e {
        IdeviceError::DeveloperModeNotEnabled => "Developer Mode is off on the iPhone: turn it on first.".to_string(),
        other => format!("Apple's developer disk image didn't go on the iPhone ({other}): is this computer online?"),
    })?;
    Ok(true)
}

async fn ddi(udid: &str, folder: &str) -> Fallible<()> {
    plugged(udid, PLUG_IN_FOR).await?;
    tell("status", "Opening the cable's link…");
    let (mut adapter, mut handshake) = by_cable(udid).await.map_err(|missed| match missed {
        Missed::Absent => "The iPhone isn't plugged in.".to_string(),
        Missed::Failed(why) => why,
    })?;
    tell("status", "Having Apple sign it for this iPhone…");
    let installed = ensure_ddi(&mut adapter, &mut handshake, folder).await?;
    tell("done", if installed { "Installed" } else { "In place" });
    Ok(())
}

async fn wifi(udid: &str, out: &str) -> Fallible<()> {
    let device = plugged(udid, PLUG_IN_FOR).await?;
    let provider = device.to_provider(UsbmuxdAddr::default(), "handset");
    // Its pairing with this computer may then be used over Wi-Fi too.
    let record = UsbmuxdConnection::default().await?.get_pair_record(udid).await?;
    let mut lockdown = LockdownClient::connect(&provider).await?;
    lockdown.start_session(&record).await?;
    lockdown
        .set_value("EnableWifiConnections", plist::Value::Boolean(true), Some("com.apple.mobile.wireless_lockdown"))
        .await?;
    tell("status", "Pairing for Wi-Fi…");
    let service = RemotePairingLockdownService::connect(&provider).await?;
    let mut remote = service.into_client(&host_name())?;
    let mut pairing = RpPairingFile::generate(&format!("Handset {}", machine_id()));
    tell("waiting", "If the iPhone asks, allow this computer");
    tokio::time::timeout(TRUST_FOR, remote.connect(&mut pairing, || async { String::new() }))
        .await
        .map_err(|_| "The iPhone didn't pair for Wi-Fi in time.")?
        .map_err(|e| format!("The iPhone didn't pair for Wi-Fi ({e})."))?;
    if let Some(parent) = std::path::Path::new(out).parent() {
        tokio::fs::create_dir_all(parent).await?;
    }
    // Only this person may read it: it lets its holder reach the phone.
    use std::io::Write;
    use std::os::unix::fs::OpenOptionsExt;
    let mut file = std::fs::OpenOptions::new().write(true).create(true).truncate(true).mode(0o600).open(out)?;
    file.write_all(&pairing.to_bytes())?;
    tell("done", "Paired for Wi-Fi: it works without the cable on the same Wi-Fi");
    Ok(())
}
