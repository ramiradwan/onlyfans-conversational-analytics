use crate::policy;
use std::{
    fs,
    io::{Read, Write},
    path::{Path, PathBuf},
    sync::Mutex,
};
use tauri::window::Color;

include!("appearance_colors.rs");

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mode {
    Light,
    Dark,
    System,
}

impl Mode {
    fn parse(value: &str) -> Option<Self> {
        match value {
            "light" => Some(Self::Light),
            "dark" => Some(Self::Dark),
            "system" => Some(Self::System),
            _ => None,
        }
    }
    fn name(self) -> &'static str {
        match self {
            Self::Light => "light",
            Self::Dark => "dark",
            Self::System => "system",
        }
    }
    fn dark(self, system_dark: bool) -> bool {
        match self {
            Self::Light => false,
            Self::Dark => true,
            Self::System => system_dark,
        }
    }
    pub fn background(self) -> Color {
        let (r, g, b) = if self.dark(system_dark()) {
            DARK_CANVAS
        } else {
            LIGHT_CANVAS
        };
        Color(r, g, b, 255)
    }
    pub fn initialization_script(self) -> String {
        format!(
            r#"(() => {{
          if (window === window.top && location.origin === 'http://bridge.localhost:17871') {{
            try {{
              if (!localStorage.getItem('mui-mode')) localStorage.setItem('mui-mode', '{}');
            }} catch {{}}
          }}
        }})();"#,
            self.name()
        )
    }
    pub fn window_theme(self) -> Option<tauri::Theme> {
        match self {
            Self::Light => Some(tauri::Theme::Light),
            Self::Dark => Some(tauri::Theme::Dark),
            Self::System => None,
        }
    }
}

// Use the Windows application appearance, not the taskbar appearance. No
// elevation, subprocess, WebView profile read or authority store is needed.
fn system_dark() -> bool {
    use windows_sys::Win32::System::Registry::*;
    let key: Vec<u16> = "Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize\0"
        .encode_utf16()
        .collect();
    let name: Vec<u16> = "AppsUseLightTheme\0".encode_utf16().collect();
    let mut value = 1u32;
    let mut size = std::mem::size_of::<u32>() as u32;
    let status = unsafe {
        RegGetValueW(
            HKEY_CURRENT_USER,
            key.as_ptr(),
            name.as_ptr(),
            RRF_RT_REG_DWORD,
            std::ptr::null_mut(),
            (&mut value as *mut u32).cast(),
            &mut size,
        )
    };
    status == 0 && size == 4 && value == 0
}

fn unique_suffix() -> String {
    format!(
        "{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos()
    )
}

pub struct Appearance {
    path: PathBuf,
    mode: Mutex<Mode>,
}
impl Appearance {
    pub fn read(data: &Path) -> Self {
        let path = data.join("appearance-mode");
        let mode = policy::safe_data_path(&path)
            .ok()
            .and_then(|path| {
                let mut text = String::new();
                fs::File::open(path)
                    .ok()?
                    .take(16)
                    .read_to_string(&mut text)
                    .ok()?;
                Mode::parse(&text)
            })
            .unwrap_or(Mode::Light);
        Self {
            path,
            mode: Mutex::new(mode),
        }
    }
    pub fn mode(&self) -> Mode {
        *self.mode.lock().unwrap_or_else(|error| error.into_inner())
    }
    pub fn select(&self, mode: Mode) -> Result<(), &'static str> {
        let mut current = self.mode.lock().map_err(|_| "appearance_unavailable")?;
        let path = policy::safe_data_path(&self.path).map_err(|_| "appearance_unavailable")?;
        // A missing mirror is migrated even if the existing MUI mode is Light.
        if *current == mode && path.is_file() {
            return Ok(());
        }
        let parent = path.parent().ok_or("appearance_unavailable")?;
        // The configured renderer is already inside the owned runtime root.
        // Do not create an alternative root from presentation input.
        if !parent.is_dir() {
            return Err("appearance_unavailable");
        }
        let temporary = parent.join(format!(".appearance-{}.tmp", unique_suffix()));
        let mut created = false;
        let result = (|| -> std::io::Result<()> {
            let mut file = fs::OpenOptions::new()
                .create_new(true)
                .write(true)
                .open(&temporary)?;
            created = true;
            file.write_all(mode.name().as_bytes())?;
            file.sync_all()?;
            drop(file);
            fs::rename(&temporary, &path)
        })();
        if result.is_err() {
            if created {
                let _ = fs::remove_file(&temporary);
            }
            return Err("appearance_unavailable");
        }
        *current = mode;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn explicit_modes_win_over_system_and_system_tracks_it() {
        for system in [false, true] {
            assert!(!Mode::Light.dark(system));
            assert!(Mode::Dark.dark(system));
            assert_eq!(Mode::System.dark(system), system);
        }
        assert_eq!(Mode::parse("dark"), Some(Mode::Dark));
        assert_eq!(Mode::parse("dark\n"), None);
        assert_eq!(Mode::parse("unknown"), None);
    }
    #[test]
    fn preference_is_read_without_creating_files_and_survives_restart() {
        let root = std::env::temp_dir().join(format!("appearance-test-{}", unique_suffix()));
        fs::create_dir(&root).unwrap();
        let owner = Appearance::read(&root);
        assert_eq!(owner.mode(), Mode::Light);
        assert!(!root.join("appearance-mode").exists());
        owner.select(Mode::Dark).unwrap();
        assert_eq!(Appearance::read(&root).mode(), Mode::Dark);
        owner.select(Mode::System).unwrap();
        assert_eq!(Appearance::read(&root).mode(), Mode::System);
        fs::write(root.join("appearance-mode"), "invalid").unwrap();
        assert_eq!(Appearance::read(&root).mode(), Mode::Light);
        fs::remove_dir_all(root).unwrap();
    }
}
