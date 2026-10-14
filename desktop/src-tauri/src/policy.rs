use std::path::{Path, PathBuf};
use url::Url;

pub const PORT: u16 = 17871;
pub const ORIGIN: &str = "http://bridge.localhost:17871";
pub const RECONFIGURE_EXIT: u32 = 75;

/// Owning the backend makes revocation precede its potentially blocking Drop.
pub struct AdmissionLease<'a, T> {
    pub backend: T,
    admitted: &'a std::sync::atomic::AtomicBool,
}
impl<'a, T> AdmissionLease<'a, T> {
    pub fn new(backend: T, admitted: &'a std::sync::atomic::AtomicBool) -> Self {
        Self { backend, admitted }
    }
}
impl<T> Drop for AdmissionLease<'_, T> {
    fn drop(&mut self) {
        self.admitted
            .store(false, std::sync::atomic::Ordering::SeqCst);
    }
}

/// App links use Chrome's canonical root path and carry a locator only.
/// Reject duplicate, encoded or unknown fields.
pub fn journey_link(value: &str) -> Result<String, &'static str> {
    let journey = value
        .strip_prefix("ofca://onboarding/?journey=")
        .ok_or("invalid_app_link")?;
    let id = uuid::Uuid::parse_str(journey).map_err(|_| "invalid_app_link")?;
    if id.hyphenated().to_string() != journey
        || id.get_version_num() != 4
        || id.get_variant() != uuid::Variant::RFC4122
    {
        return Err("invalid_app_link");
    }
    Ok(journey.to_owned())
}

pub fn launch_journey(args: &[String]) -> Result<Option<String>, &'static str> {
    match args {
        [_executable] => Ok(None),
        [_executable, flag, value] if flag == "--open-workspace" => journey_link(value).map(Some),
        _ => Err("invalid_launch_arguments"),
    }
}

pub fn local_document(url: &Url) -> bool {
    url.scheme() == "http"
        && url.host_str() == Some("bridge.localhost")
        && url.port() == Some(PORT)
        && url.username().is_empty()
        && url.password().is_none()
        && (matches!(
            url.path(),
            "/" | "/inbox" | "/analytics" | "/graph-explorer" | "/settings"
        ) || url.path() == "/onboarding"
            || url.path().starts_with("/onboarding/"))
}

pub fn shell_document(url: &Url) -> bool {
    url.scheme() == "http"
        && url.host_str() == Some("tauri.localhost")
        && url.port().is_none()
        && url.username().is_empty()
        && url.password().is_none()
        && url.query().is_none()
        && url.fragment().is_none()
        && matches!(url.path(), "/" | "/index.html" | "/unavailable.html")
}

pub fn window_controls(label: &str, url: &Url, admitted: bool, quitting: bool) -> bool {
    label == "main" && !quitting && (shell_document(url) || (admitted && local_document(url)))
}

/// Noncreating resolution: aliases and installation containment fail before writes.
fn safe_data_path(path: &Path) -> Result<PathBuf, &'static str> {
    use std::path::Component;
    if !path.is_absolute() {
        return Err("invalid_data_directory");
    }
    let mut current = PathBuf::new();
    for part in path.components() {
        if matches!(part, Component::ParentDir | Component::CurDir) {
            return Err("invalid_data_directory");
        }
        #[cfg(windows)]
        if let Component::Prefix(prefix) = part {
            if matches!(
                prefix.kind(),
                std::path::Prefix::UNC(_, _)
                    | std::path::Prefix::VerbatimUNC(_, _)
                    | std::path::Prefix::DeviceNS(_)
            ) {
                return Err("invalid_data_directory");
            }
        }
        current.push(part);
        match std::fs::symlink_metadata(&current) {
            Ok(metadata) => {
                #[cfg(windows)]
                let reparse = {
                    use std::os::windows::fs::MetadataExt;
                    metadata.file_attributes() & 0x400 != 0
                };
                #[cfg(not(windows))]
                let reparse = false;
                if metadata.file_type().is_symlink() || reparse {
                    return Err("invalid_data_directory");
                }
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => (),
            Err(_) => return Err("data_directory_unavailable"),
        }
    }
    let mut ancestor = current.clone();
    let mut missing = Vec::new();
    while !ancestor.exists() {
        missing.push(
            ancestor
                .file_name()
                .ok_or("invalid_data_directory")?
                .to_os_string(),
        );
        ancestor.pop();
    }
    let mut resolved = ancestor
        .canonicalize()
        .map_err(|_| "data_directory_unavailable")?;
    for part in missing.into_iter().rev() {
        resolved.push(part);
    }
    #[cfg(windows)]
    {
        resolved = dunce::simplified(&resolved).to_path_buf();
    }
    Ok(resolved)
}

pub fn data_directory(image: &Path) -> Result<PathBuf, &'static str> {
    #[cfg(windows)]
    let release = crate::deployment::load(
        &image
            .parent()
            .ok_or("invalid_installation")?
            .join("desktop-release.json"),
        crate::windows::package_identity()?.as_ref(),
    )?;
    #[cfg(not(windows))]
    let release: Option<crate::deployment::DesktopRelease> = None;
    let product =
        PathBuf::from(std::env::var_os("LOCALAPPDATA").ok_or("data_directory_unavailable")?)
            .join("OnlyFans Conversational Analytics");
    let path = if let Some(value) = std::env::var_os("LOCAL_ANALYTICS_DATA_DIR") {
        PathBuf::from(value)
    } else {
        release.as_ref().map_or_else(
            || product.clone(),
            |release| release.default_data_root(&product),
        )
    };
    let path = safe_data_path(&path)?;
    if let Some(release) = &release {
        release.validate_override(&path, &safe_data_path(&product)?)?;
    }
    let installed = image
        .parent()
        .ok_or("invalid_installation")?
        .canonicalize()
        .map_err(|_| "invalid_installation")?;
    #[cfg(windows)]
    let installed = dunce::simplified(&installed);
    if path.starts_with(installed) {
        return Err("invalid_data_directory");
    }
    Ok(path)
}

pub fn webview_directory(data: &Path) -> Result<PathBuf, &'static str> {
    safe_data_path(&data.join("webview"))
}

#[derive(Clone, Debug, PartialEq)]
pub struct Listener {
    pub address: std::net::IpAddr,
    pub pid: u32,
    pub image: PathBuf,
    pub sid: Vec<u8>,
}

pub fn verify_listener(
    listeners: &[Listener],
    pid: u32,
    image: &Path,
    sid: &[u8],
) -> Result<bool, &'static str> {
    match listeners {
        [] => Ok(false),
        [owner]
            if owner.address == std::net::IpAddr::V4(std::net::Ipv4Addr::LOCALHOST)
                && owner.pid == pid
                && owner.image == image
                && owner.sid == sid =>
        {
            Ok(true)
        }
        _ => Err("port_conflict"),
    }
}

#[derive(Default)]
pub struct RestartPolicy {
    reconfigured: bool,
    quitting: bool,
}
impl RestartPolicy {
    pub fn quit(&mut self) {
        self.quitting = true;
    }
    pub fn should_restart(&mut self, code: u32, configured: bool) -> bool {
        if !self.quitting && !self.reconfigured && code == RECONFIGURE_EXIT && configured {
            self.reconfigured = true;
            true
        } else {
            false
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn resolving_a_new_data_and_webview_root_creates_nothing_and_rejects_aliases() {
        let mut nonce = [0u8; 16];
        getrandom::fill(&mut nonce).unwrap();
        let suffix: String = nonce.iter().map(|value| format!("{value:02x}")).collect();
        let root = std::env::temp_dir().join(format!("ofca-path-contract-{suffix}"));
        let candidate = root.join("new").join("data");
        assert!(safe_data_path(&candidate).is_ok());
        assert!(webview_directory(&candidate).is_ok());
        assert!(!root.exists());
        assert!(safe_data_path(&root.join("..").join("escape")).is_err());
        std::fs::create_dir_all(&root).unwrap();
        let target = root.join("real");
        std::fs::create_dir(&target).unwrap();
        let alias = root.join("alias");
        #[cfg(windows)]
        std::os::windows::fs::symlink_dir(&target, &alias)
            .expect("Windows qualification requires symlink permission");
        #[cfg(not(windows))]
        std::os::unix::fs::symlink(&target, &alias).unwrap();
        assert!(safe_data_path(&alias.join("data")).is_err());
        assert!(webview_directory(&alias).is_err());
        #[cfg(windows)]
        std::fs::remove_dir(&alias).unwrap();
        #[cfg(not(windows))]
        std::fs::remove_file(&alias).unwrap();
        std::fs::remove_dir(&target).unwrap();
        std::fs::remove_dir(&root).unwrap();
    }
    #[test]
    fn every_error_revokes_admission_before_slow_backend_teardown() {
        use std::sync::{
            atomic::{AtomicBool, Ordering},
            mpsc,
        };
        struct SlowBackend<'a>(&'a AtomicBool, mpsc::Sender<bool>);
        impl Drop for SlowBackend<'_> {
            fn drop(&mut self) {
                self.1.send(self.0.load(Ordering::SeqCst)).unwrap();
                std::thread::sleep(std::time::Duration::from_millis(100));
            }
        }
        for error in [
            "port_conflict",
            "listener_lost",
            "owner_inspection_failed",
            "handoff_rejected",
            "window_unavailable",
        ] {
            let admitted = AtomicBool::new(true);
            let (send, receive) = mpsc::channel::<bool>();
            std::thread::scope(|scope| {
                let observer = scope.spawn(move || {
                    assert!(
                        !receive.recv().unwrap(),
                        "{error} retained native authority during shutdown"
                    )
                });
                let fail = || -> Result<(), &str> {
                    let _lease = AdmissionLease::new(SlowBackend(&admitted, send), &admitted);
                    Err(error)?;
                    Ok(())
                };
                assert_eq!(fail(), Err(error));
                observer.join().unwrap();
            });
        }
    }
    #[test]
    fn links_are_canonical_navigation_only() {
        let base = "ofca://onboarding/?journey=aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
        assert!(journey_link(base).is_ok());
        for value in [
            format!("{base}&creator=123"),
            format!("{base}#token=x"),
            base.replace("journey=", "journey=%61"),
            base.replace("onboarding", "evil@onboarding"),
            base.replace("onboarding/", "onboarding"),
            base.replace("onboarding/", "onboarding//"),
            base.replace("onboarding/", "onboarding/other"),
            base.replace("onboarding/", "onboarding:17871/"),
            base.replace("aaaa", "AAAA"),
        ] {
            assert!(journey_link(&value).is_err());
        }
        assert!(launch_journey(&["Bridge.exe".into(), "--brain".into()]).is_err());
    }
    #[test]
    fn remote_and_unexpected_documents_have_no_native_navigation() {
        assert!(local_document(
            &Url::parse(&format!("{ORIGIN}/onboarding")).unwrap()
        ));
        // React BrowserRouter changes the owned document's path in place.
        // Keep these exact application routes usable without admitting local
        // helpers, API endpoints, or arbitrary same-origin documents.
        for path in ["/", "/inbox", "/analytics", "/graph-explorer", "/settings"] {
            let url = Url::parse(&format!("{ORIGIN}{path}")).unwrap();
            assert!(local_document(&url));
            assert!(window_controls("main", &url, true, false));
            assert!(!window_controls("main", &url, false, false));
            assert!(!window_controls("other", &url, true, false));
            assert!(!window_controls("main", &url, true, true));
        }
        for value in [
            "https://bridge.localhost:17871/",
            "http://127.0.0.1:17871/",
            "http://bridge.localhost:17871@evil.example/",
            "http://bridge.localhost:17871/browser-tasks/entry",
            "http://bridge.localhost:17871/setup-code",
            "http://bridge.localhost:17871/provisioning",
            "http://bridge.localhost:17871/provisioning/handoff?code=abc",
            "http://bridge.localhost:17871/api/v1/session/handoff",
            "file:///C:/test.html",
            "http://bridge.localhost:17872/",
            "http://bridge.localhost:17871/api/v1/secret",
            "http://bridge.localhost:17871/settings/other",
            "http://bridge.localhost:17871/settings.html",
            "http://bridge.localhost:17871/inbox/other",
            "http://bridge.localhost:17871/analytics/other",
            "http://bridge.localhost:17871/graph-explorer/other",
            "http://bridge.localhost:17871/unknown",
            "http://bridge.localhost:17872/settings",
            "http://user@bridge.localhost:17871/settings",
            "https://bridge.localhost:17871/settings",
        ] {
            assert!(!local_document(&Url::parse(value).unwrap()));
        }
    }
    #[test]
    fn listener_admission_requires_exact_child_image_user_and_address() {
        let image = PathBuf::from("Brain.exe");
        let owner = Listener {
            address: "127.0.0.1".parse().unwrap(),
            pid: 42,
            image: image.clone(),
            sid: vec![1, 2],
        };
        assert_eq!(verify_listener(&[], 42, &image, &[1, 2]), Ok(false));
        assert_eq!(
            verify_listener(std::slice::from_ref(&owner), 42, &image, &[1, 2]),
            Ok(true)
        );
        for hostile in [
            Listener {
                pid: 43,
                ..owner.clone()
            },
            Listener {
                image: "Other.exe".into(),
                ..owner.clone()
            },
            Listener {
                sid: vec![3],
                ..owner.clone()
            },
            Listener {
                address: "0.0.0.0".parse().unwrap(),
                ..owner.clone()
            },
            Listener {
                address: "::1".parse().unwrap(),
                ..owner.clone()
            },
        ] {
            assert_eq!(
                verify_listener(&[hostile], 42, &image, &[1, 2]),
                Err("port_conflict")
            );
        }
        assert!(verify_listener(&[owner.clone(), owner], 42, &image, &[1, 2]).is_err());
    }
    #[test]
    fn window_controls_require_own_shell_or_live_local_admission() {
        for path in ["/", "/index.html", "/unavailable.html"] {
            let url = Url::parse(&format!("http://tauri.localhost{path}")).unwrap();
            assert!(window_controls("main", &url, false, false));
            assert!(!window_controls("other", &url, true, false));
            assert!(!window_controls("main", &url, true, true));
        }
        let local = Url::parse(ORIGIN).unwrap();
        assert!(window_controls("main", &local, true, false));
        assert!(!window_controls("main", &local, false, false));
        for denied in [
            "http://tauri.localhost/unavailable.html?redirect=x",
            "http://tauri.localhost/other.html",
            "http://tauri.localhost:17871/",
            "http://user@tauri.localhost/",
            "http://bridge.localhost:17871/browser-tasks/entry",
            "https://provider.example/",
            "http://127.0.0.1:17871/",
        ] {
            assert!(!window_controls(
                "main",
                &Url::parse(denied).unwrap(),
                true,
                false
            ));
        }
    }
    #[test]
    fn only_one_completed_configuration_restart_is_allowed() {
        let mut policy = RestartPolicy::default();
        assert!(!policy.should_restart(1, true));
        assert!(!policy.should_restart(75, false));
        assert!(policy.should_restart(75, true));
        assert!(!policy.should_restart(75, true));
        let mut quitting = RestartPolicy::default();
        quitting.quit();
        assert!(!quitting.should_restart(75, true));
    }
}
