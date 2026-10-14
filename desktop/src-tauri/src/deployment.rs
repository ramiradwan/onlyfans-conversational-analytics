//! Closed immutable desktop metadata. Windows identity is supplied by the OS.
use serde::Deserialize;
use std::path::{Path, PathBuf};

#[derive(Clone, Debug, PartialEq)]
pub struct PackageIdentity {
    pub name: String,
    pub publisher: String,
    pub version: String,
    pub architecture: String,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Identity {
    pub name: String,
    pub publisher: String,
    pub publisher_display_name: String,
    pub application_id: String,
    pub architecture: String,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DesktopRelease {
    schema: String,
    pub purpose: String,
    pub identity: Identity,
    application_version: String,
    package_version: String,
    profile_sha256: String,
    pub update_owner: String,
    #[serde(deserialize_with = "required_optional_uri")]
    pub app_installer_uri: Option<String>,
    storage_policy: String,
    executable_family: String,
    pub webview2_minimum_version: String,
}

fn version(text: &str) -> bool {
    let parts: Vec<_> = text.split('.').collect();
    parts.len() == 4
        && parts.iter().all(|part| {
            part.parse::<u16>()
                .is_ok_and(|value| value.to_string() == *part)
        })
}

fn required_optional_uri<'de, D: serde::Deserializer<'de>>(
    deserializer: D,
) -> Result<Option<String>, D::Error> {
    Option::<String>::deserialize(deserializer)
}

impl DesktopRelease {
    pub fn validate(&self, actual: &PackageIdentity) -> Result<(), &'static str> {
        let identity = &self.identity;
        let safe_string = |value: &str| {
            !value.is_empty()
                && value.trim() == value
                && !value.chars().any(|character| character < ' ')
        };
        if self.schema != "ofca-desktop-release/v1"
            || !matches!(self.purpose.as_str(), "production" | "qualification")
            || self.storage_policy != "retained-product-root/v1"
            || self.executable_family != "brain-bridge/v1"
            || self.application_version != env!("CARGO_PKG_VERSION")
            || !version(&self.package_version)
            || !version(&self.webview2_minimum_version)
            || self.profile_sha256.len() != 64
            || !self
                .profile_sha256
                .bytes()
                .all(|value| value.is_ascii_digit() || (b'a'..=b'f').contains(&value))
            || !(3..=50).contains(&identity.name.len())
            || !identity
                .name
                .bytes()
                .all(|value| value.is_ascii_alphanumeric() || b".-".contains(&value))
            || !identity.publisher.starts_with("CN=")
            || identity.publisher.len() > 8192
            || identity.publisher_display_name.chars().count() > 256
            || identity.application_id.len() > 64
            || !identity
                .application_id
                .as_bytes()
                .first()
                .is_some_and(u8::is_ascii_alphabetic)
            || !identity
                .application_id
                .bytes()
                .all(|value| value.is_ascii_alphanumeric())
            || [
                &identity.name,
                &identity.publisher,
                &identity.publisher_display_name,
                &identity.application_id,
                &identity.architecture,
            ]
            .iter()
            .any(|value| !safe_string(value))
        {
            return Err("desktop_descriptor_invalid");
        }
        if identity.name != actual.name
            || identity.publisher != actual.publisher
            || self.package_version != actual.version
            || identity.architecture != "x64"
            || actual.architecture != "x64"
        {
            return Err("desktop_identity_mismatch");
        }
        let synthetic_name = identity
            .name
            .to_ascii_lowercase()
            .starts_with("ofca.qualification.");
        let synthetic_publisher = identity
            .publisher
            .to_ascii_lowercase()
            .starts_with("cn=ofca msix qualification");
        if (self.purpose == "qualification" && !(synthetic_name && synthetic_publisher))
            || (self.purpose == "production" && (synthetic_name || synthetic_publisher))
        {
            return Err("desktop_descriptor_invalid");
        }
        match (
            self.update_owner.as_str(),
            self.app_installer_uri.as_deref(),
        ) {
            ("microsoft-store", None) => (),
            ("direct-appinstaller", Some(value)) => {
                let uri = url::Url::parse(value).map_err(|_| "desktop_descriptor_invalid")?;
                if value.len() > 2048
                    || value.contains('\\')
                    || value.chars().any(|character| character <= ' ')
                    || uri.scheme() != "https"
                    || uri.host_str().is_none()
                    || !uri.username().is_empty()
                    || uri.password().is_some()
                    || uri.port_or_known_default() != Some(443)
                    || uri.query().is_some()
                    || uri.fragment().is_some()
                    || !uri.path().ends_with(".appinstaller")
                {
                    return Err("desktop_descriptor_invalid");
                }
            }
            _ => return Err("desktop_descriptor_invalid"),
        }
        Ok(())
    }

    pub fn default_data_root(&self, product: &Path) -> PathBuf {
        if self.purpose == "qualification" {
            product.join("qualification").join(&self.identity.name)
        } else {
            product.to_path_buf()
        }
    }

    pub fn validate_override(&self, candidate: &Path, product: &Path) -> Result<(), &'static str> {
        let fold = |path: &Path| PathBuf::from(path.to_string_lossy().to_lowercase());
        let candidate = fold(candidate);
        let product = fold(product);
        let own = fold(&self.default_data_root(&product));
        if self.purpose == "qualification"
            && (candidate == product
                || (candidate.starts_with(&product) && !candidate.starts_with(own)))
        {
            return Err("qualification_root_conflict");
        }
        Ok(())
    }
}

pub fn load(
    path: &Path,
    actual: Option<&PackageIdentity>,
) -> Result<Option<DesktopRelease>, &'static str> {
    if !path.exists() {
        return if actual.is_some() {
            Err("packaged_descriptor_missing")
        } else {
            Ok(None)
        };
    }
    let actual = actual.ok_or("descriptor_without_package_identity")?;
    let metadata = path.metadata().map_err(|_| "desktop_descriptor_invalid")?;
    if !metadata.is_file() || metadata.len() > 65536 {
        return Err("desktop_descriptor_invalid");
    }
    let release: DesktopRelease =
        serde_json::from_slice(&std::fs::read(path).map_err(|_| "desktop_descriptor_invalid")?)
            .map_err(|_| "desktop_descriptor_invalid")?;
    release.validate(actual)?;
    Ok(Some(release))
}

#[cfg(test)]
mod tests {
    use super::*;
    fn actual(name: &str, publisher: &str) -> PackageIdentity {
        PackageIdentity {
            name: name.into(),
            publisher: publisher.into(),
            version: "1.0.1.0".into(),
            architecture: "x64".into(),
        }
    }
    #[test]
    fn package_owned_fixtures_match_native_contract_and_independent_profiles() {
        for (text, identity) in [
            (
                include_str!("../../../shared/desktop-release/qualification-direct.json"),
                actual(
                    "OFCA.Qualification.Direct",
                    "CN=OFCA MSIX Qualification Direct",
                ),
            ),
            (
                include_str!("../../../shared/desktop-release/qualification-store.json"),
                actual(
                    "OFCA.Qualification.Store",
                    "CN=OFCA MSIX Qualification Store",
                ),
            ),
        ] {
            let release: DesktopRelease = serde_json::from_str(text).unwrap();
            assert_eq!(release.validate(&identity), Ok(()));
            let product = Path::new("C:/retained-product");
            let own = release.default_data_root(product);
            assert_eq!(own, product.join("qualification").join(&identity.name));
            assert!(release.validate_override(product, product).is_err());
            assert!(release
                .validate_override(&product.join("another"), product)
                .is_err());
            assert!(release.validate_override(&own, product).is_ok());
            assert!(release
                .validate_override(Path::new("C:/isolated-test-root"), product)
                .is_ok());
        }
    }
    #[test]
    fn identity_policy_and_unknown_fields_cannot_change_delivery_or_storage() {
        let text = include_str!("../../../shared/desktop-release/qualification-direct.json");
        let identity = actual(
            "OFCA.Qualification.Direct",
            "CN=OFCA MSIX Qualification Direct",
        );
        for (field, value) in [
            ("purpose", serde_json::json!("production")),
            ("package_version", serde_json::json!("1.0.2.0")),
            ("update_owner", serde_json::json!("legacy-inno")),
            ("update_owner", serde_json::json!("microsoft-store")),
            ("storage_policy", serde_json::json!("package-local")),
            (
                "app_installer_uri",
                serde_json::json!("http://qualification.example.test/feed.appinstaller"),
            ),
        ] {
            let mut modified: serde_json::Value = serde_json::from_str(text).unwrap();
            modified[field] = value;
            let release: DesktopRelease = serde_json::from_value(modified).unwrap();
            assert!(release.validate(&identity).is_err(), "{field}");
        }
        let mut modified: serde_json::Value = serde_json::from_str(text).unwrap();
        modified["extra"] = serde_json::json!(true);
        assert!(serde_json::from_value::<DesktopRelease>(modified).is_err());
        let mut missing: serde_json::Value = serde_json::from_str(text).unwrap();
        missing.as_object_mut().unwrap().remove("app_installer_uri");
        assert!(serde_json::from_value::<DesktopRelease>(missing).is_err());
        assert!(serde_json::from_str::<DesktopRelease>(&text.replacen(
            '{',
            "{\"purpose\":\"qualification\",",
            1
        ))
        .is_err());
    }
}
