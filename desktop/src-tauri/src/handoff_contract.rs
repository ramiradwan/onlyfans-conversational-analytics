//! Private native servicing evidence. It carries no application authority.
use serde::{Deserialize, Serialize};

pub const MAX_FRAME: usize = 8192;
pub const EXIT_FOR_SERVICING: u32 = 73;

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Phase {
    Prepared,
    Committed,
    Completed,
    Cancelled,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Intent {
    pub schema: String,
    pub session: String,
    pub name: String,
    pub publisher: String,
    pub family: String,
    pub source_version: String,
    pub target_version: String,
    pub target_full_name: String,
    pub app_installer_uri: String,
    pub source_sha256: String,
    pub phase: Phase,
}

pub fn version(text: &str) -> Result<[u16; 4], &'static str> {
    let values: Vec<_> = text.split('.').collect();
    if values.len() != 4 {
        return Err("servicing_intent_invalid");
    }
    let mut result = [0u16; 4];
    for (value, slot) in values.iter().zip(result.iter_mut()) {
        *slot = value.parse().map_err(|_| "servicing_intent_invalid")?;
        if slot.to_string() != *value {
            return Err("servicing_intent_invalid");
        }
    }
    if result[0] != 1 || result[3] != 0 {
        return Err("servicing_intent_invalid");
    }
    Ok(result)
}

pub fn hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
}

impl Intent {
    pub fn decode(bytes: &[u8]) -> Result<Self, &'static str> {
        if bytes.is_empty() || bytes.len() > MAX_FRAME {
            return Err("servicing_intent_invalid");
        }
        let value: Self = serde_json::from_slice(bytes).map_err(|_| "servicing_intent_invalid")?;
        value.validate()?;
        Ok(value)
    }

    pub fn validate(&self) -> Result<(), &'static str> {
        let Some((name, publisher_id)) = self.family.rsplit_once('_') else {
            return Err("servicing_intent_invalid");
        };
        if self.schema != "ofca-native-servicing/v1"
            || !hex(&self.session, 32)
            || !hex(&self.source_sha256, 64)
            || name != self.name
            || publisher_id.len() != 13
            || !publisher_id
                .bytes()
                .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit())
            || !(3..=50).contains(&self.name.len())
            || !self
                .name
                .bytes()
                .all(|c| c.is_ascii_alphanumeric() || b".-".contains(&c))
            || !self.publisher.starts_with("CN=")
            || self.publisher.len() > 4096
            || self.publisher.chars().any(char::is_control)
            || version(&self.target_version)? <= version(&self.source_version)?
            || self.target_full_name
                != format!(
                    "{}_{}_x64__{}",
                    self.name, self.target_version, publisher_id
                )
        {
            return Err("servicing_intent_invalid");
        }
        let uri =
            url::Url::parse(&self.app_installer_uri).map_err(|_| "servicing_intent_invalid")?;
        if uri.scheme() != "https"
            || uri.port_or_known_default() != Some(443)
            || !uri.username().is_empty()
            || uri.password().is_some()
            || uri.query().is_some()
            || uri.fragment().is_some()
            || uri.as_str() != self.app_installer_uri
            || !uri.path().ends_with(".appinstaller")
        {
            return Err("servicing_intent_invalid");
        }
        Ok(())
    }

    pub fn matches_release(&self, release: &crate::deployment::DesktopRelease) -> bool {
        self.name == release.identity.name
            && self.publisher == release.identity.publisher
            && release.identity.application_id == "Bridge"
            && release.update_owner == "direct-appinstaller"
            && release.app_installer_uri.as_deref() == Some(&self.app_installer_uri)
    }

    pub fn admits(&self, actual_version: &str) -> Result<bool, &'static str> {
        self.validate()?;
        let actual = version(actual_version)?;
        Ok(
            (self.phase == Phase::Cancelled && actual >= version(&self.source_version)?)
                || actual >= version(&self.target_version)?,
        )
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    pub fn intent() -> Intent {
        Intent {
            schema: "ofca-native-servicing/v1".into(),
            session: "a".repeat(32),
            name: "OFCA.Qualification.Direct".into(),
            publisher: "CN=OFCA MSIX Qualification Direct".into(),
            family: "OFCA.Qualification.Direct_123456789abcd".into(),
            source_version: "1.0.1.0".into(),
            target_version: "1.0.2.0".into(),
            target_full_name: "OFCA.Qualification.Direct_1.0.2.0_x64__123456789abcd".into(),
            app_installer_uri:
                "https://qualification.example.test/desktop/direct/Dipsy.appinstaller".into(),
            source_sha256: "b".repeat(64),
            phase: Phase::Committed,
        }
    }
    #[test]
    fn pending_target_never_admits_old_code_but_allows_a_forward_corrective_release() {
        let mut value = intent();
        assert_eq!(value.admits("1.0.1.0"), Ok(false));
        assert_eq!(value.admits("1.0.2.0"), Ok(true));
        assert_eq!(value.admits("1.0.3.0"), Ok(true));
        value.phase = Phase::Prepared;
        assert_eq!(value.admits("1.0.1.0"), Ok(false));
        value.phase = Phase::Cancelled;
        assert_eq!(value.admits("1.0.1.0"), Ok(true));
    }
    #[test]
    fn closed_evidence_rejects_ambiguity_identity_switches_and_invalid_versions() {
        let value = intent();
        let bytes = serde_json::to_vec(&value).unwrap();
        assert_eq!(Intent::decode(&bytes), Ok(value.clone()));
        let text = String::from_utf8(bytes).unwrap();
        for invalid in [
            text.replace("\"schema\":", "\"schema\":\"other\",\"schema\":"),
            text.replace("\"phase\":", "\"authority\":true,\"phase\":"),
            text.replace("1.0.2.0_x64", "1.0.1.0_x64"),
            text.replace("1.0.2.0", "1.0.02.0"),
            text.replace("123456789abcd", "../another"),
            text.replace("https://qualification", "http://qualification"),
        ] {
            assert!(Intent::decode(invalid.as_bytes()).is_err());
        }
        assert!(Intent::decode(&vec![b'x'; MAX_FRAME + 1]).is_err());
        let release: crate::deployment::DesktopRelease = serde_json::from_str(include_str!(
            "../../../shared/desktop-release/qualification-direct.json"
        ))
        .unwrap();
        assert!(value.matches_release(&release));
        let mut changed = value;
        changed.publisher.push(' ');
        assert!(!changed.matches_release(&release));
    }
}
