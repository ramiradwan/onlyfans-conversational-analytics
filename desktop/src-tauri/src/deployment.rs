//! Closed immutable desktop metadata. Windows identity is supplied by the OS.
use serde::Deserialize;
use std::path::{Path, PathBuf};

#[derive(Clone, Debug, PartialEq)]
pub struct PackageIdentity {
    pub name: String,
    pub publisher: String,
    pub version: String,
    pub architecture: String,
    pub family: Option<String>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct InstanceNames {
    pub root_mutex: String,
    pub plugin_id: String,
}

/// The root signal fences presentation writers too. The activation namespace
/// also binds user and delivery identity, without a versioned executable path.
pub fn instance_names(
    root: &Path,
    sid: &[u8],
    release: Option<&DesktopRelease>,
) -> Result<InstanceNames, &'static str> {
    use sha2::{Digest, Sha256};
    let root = root
        .to_str()
        .ok_or("invalid_data_directory")?
        .to_lowercase();
    let root_key =
        serde_json::to_vec(&("desktop-root/v1", &root)).map_err(|_| "invalid_installation")?;
    let identity = release.map(|r| {
        (
            &r.purpose,
            &r.identity.name,
            &r.identity.publisher,
            &r.identity.architecture,
            &r.identity.application_id,
            &r.update_owner,
            &r.app_installer_uri,
        )
    });
    let instance_key = serde_json::to_vec(&("desktop-instance/v1", &root, sid, identity))
        .map_err(|_| "invalid_installation")?;
    Ok(InstanceNames {
        root_mutex: format!("Global\\OFCA.NativeRoot.v1.{:x}", Sha256::digest(root_key)),
        plugin_id: format!(
            "org.conversation-analytics.bridge.instance.{:x}",
            Sha256::digest(instance_key)
        ),
    })
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

/// Compile the approved identities from the same profiles used by packaging.
/// Package versions and executable paths do not identify a competing channel.
pub fn production_package_identities() -> Result<Vec<Identity>, &'static str> {
    #[derive(Deserialize)]
    struct Profile {
        purpose: String,
        identity: Identity,
    }
    [
        include_str!("../../../packaging/msix/profiles/production-direct.json"),
        include_str!("../../../packaging/msix/profiles/production-store.json"),
    ]
    .iter()
    .map(|bytes| {
        let profile: Profile = serde_json::from_str(bytes).map_err(|_| "invalid_installation")?;
        if profile.purpose != "production" {
            return Err("invalid_installation");
        }
        Ok(profile.identity)
    })
    .collect()
}

/// Only customer-root launches can conflict with these registered channels.
/// The caller supplies the OS query, never renderer input or a process list.
pub fn validate_msix_coexistence(
    release: Option<&DesktopRelease>,
    customer_root: bool,
    mut registered: impl FnMut(&str, &str) -> Result<bool, &'static str>,
) -> Result<(), &'static str> {
    if !customer_root || release.is_some_and(|r| r.purpose == "qualification") {
        return Ok(());
    }
    for identity in production_package_identities()? {
        if release.is_some_and(|r| {
            r.identity.name == identity.name && r.identity.publisher == identity.publisher
        }) {
            continue;
        }
        if registered(&identity.name, &identity.publisher)
            .map_err(|_| "runtime_installation_conflict")?
        {
            return Err("runtime_installation_conflict");
        }
    }
    Ok(())
}

#[derive(Clone, Debug)]
pub struct DesktopRelease {
    schema: String,
    pub purpose: String,
    pub identity: Identity,
    application_version: String,
    package_version: String,
    profile_sha256: String,
    pub update_owner: String,
    pub app_installer_uri: Option<String>,
    storage_policy: String,
    executable_family: String,
    pub webview2_minimum_version: String,
    pub servicing: Option<Servicing>,
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct EngineWindowsIdentity {
    pub name: String,
    pub publisher: String,
    pub family: String,
    pub architecture: String,
}
#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct EngineRelease {
    pub identity: EngineWindowsIdentity,
    pub package_version: String,
}
#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum InstalledArtifact {
    MainPackage {},
    RelatedBundle {
        related_set_id: String,
        engine: EngineRelease,
    },
}
#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum AllowedTargets {
    MainPackageOnly {},
    MainPackageAndRelatedBundle {
        engine_identity: EngineWindowsIdentity,
    },
}
#[derive(Clone, Debug, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Servicing {
    pub installed_artifact: InstalledArtifact,
    pub allowed_targets: AllowedTargets,
}

impl<'de> Deserialize<'de> for DesktopRelease {
    fn deserialize<D: serde::Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        #[derive(Deserialize)]
        #[serde(tag = "schema", deny_unknown_fields)]
        enum Wire {
            #[serde(rename = "ofca-desktop-release/v1")]
            V1 {
                purpose: String,
                identity: Identity,
                application_version: String,
                package_version: String,
                profile_sha256: String,
                update_owner: String,
                #[serde(deserialize_with = "required_optional_uri")]
                app_installer_uri: Option<String>,
                storage_policy: String,
                executable_family: String,
                webview2_minimum_version: String,
            },
            #[serde(rename = "ofca-desktop-release/v2")]
            V2 {
                purpose: String,
                identity: Identity,
                application_version: String,
                package_version: String,
                profile_sha256: String,
                update_owner: String,
                #[serde(deserialize_with = "required_optional_uri")]
                app_installer_uri: Option<String>,
                storage_policy: String,
                executable_family: String,
                webview2_minimum_version: String,
                servicing: Servicing,
            },
        }
        Ok(match Wire::deserialize(d)? {
            Wire::V1 {
                purpose,
                identity,
                application_version,
                package_version,
                profile_sha256,
                update_owner,
                app_installer_uri,
                storage_policy,
                executable_family,
                webview2_minimum_version,
            } => Self {
                schema: "ofca-desktop-release/v1".into(),
                purpose,
                identity,
                application_version,
                package_version,
                profile_sha256,
                update_owner,
                app_installer_uri,
                storage_policy,
                executable_family,
                webview2_minimum_version,
                servicing: None,
            },
            Wire::V2 {
                purpose,
                identity,
                application_version,
                package_version,
                profile_sha256,
                update_owner,
                app_installer_uri,
                storage_policy,
                executable_family,
                webview2_minimum_version,
                servicing,
            } => Self {
                schema: "ofca-desktop-release/v2".into(),
                purpose,
                identity,
                application_version,
                package_version,
                profile_sha256,
                update_owner,
                app_installer_uri,
                storage_policy,
                executable_family,
                webview2_minimum_version,
                servicing: Some(servicing),
            },
        })
    }
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
        self.validate_context(actual, Some(env!("CARGO_PKG_VERSION")))
    }
    pub fn validate_target(&self, actual: &PackageIdentity) -> Result<(), &'static str> {
        self.validate_context(actual, None)
    }
    fn validate_context(
        &self,
        actual: &PackageIdentity,
        running: Option<&str>,
    ) -> Result<(), &'static str> {
        let identity = &self.identity;
        let safe_string = |value: &str| {
            !value.is_empty()
                && value.trim() == value
                && !value.chars().any(|character| character < ' ')
        };
        if !matches!(
            self.schema.as_str(),
            "ofca-desktop-release/v1" | "ofca-desktop-release/v2"
        ) || !matches!(self.purpose.as_str(), "production" | "qualification")
            || self.storage_policy != "retained-product-root/v1"
            || self.executable_family != "brain-bridge/v1"
            || running.is_some_and(|build| self.application_version != build)
            || (running.is_none() && !application_version(&self.application_version))
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
        if let Some(servicing) = &self.servicing {
            actual_publisher_id(actual)?;
            if self.update_owner != "direct-appinstaller" {
                return Err("desktop_descriptor_invalid");
            }
            let engine = match &servicing.allowed_targets {
                AllowedTargets::MainPackageOnly { .. } => None,
                AllowedTargets::MainPackageAndRelatedBundle { engine_identity } => {
                    Some(engine_identity)
                }
            };
            if let Some(engine) = engine {
                engine.validate(actual)?;
            }
            if let InstalledArtifact::RelatedBundle {
                related_set_id,
                engine: installed,
            } = &servicing.installed_artifact
            {
                if engine != Some(&installed.identity)
                    || !identifier(related_set_id)
                    || !engine_version(&installed.package_version)
                {
                    return Err("desktop_descriptor_invalid");
                }
            }
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

    pub fn engine_identity(&self) -> Option<&EngineWindowsIdentity> {
        match &self.servicing.as_ref()?.allowed_targets {
            AllowedTargets::MainPackageOnly { .. } => None,
            AllowedTargets::MainPackageAndRelatedBundle { engine_identity } => {
                Some(engine_identity)
            }
        }
    }
    pub fn allows_target(&self, related: bool) -> bool {
        match &self.servicing {
            None => !related,
            Some(s) => match (&s.installed_artifact, &s.allowed_targets) {
                (InstalledArtifact::RelatedBundle { .. }, _) => {
                    related && self.engine_identity().is_some()
                }
                (_, AllowedTargets::MainPackageOnly { .. }) => !related,
                _ => true,
            },
        }
    }
    pub fn target_matches(&self, current: &Self) -> bool {
        self.purpose == current.purpose
            && self.identity.name == current.identity.name
            && self.identity.publisher == current.identity.publisher
            && self.identity.architecture == current.identity.architecture
            && self.identity.application_id == current.identity.application_id
            && self.update_owner == current.update_owner
            && self.app_installer_uri == current.app_installer_uri
            && self.storage_policy == current.storage_policy
    }
}

pub fn engine_version(text: &str) -> bool {
    version(text)
        && text
            .split('.')
            .next()
            .and_then(|x| x.parse::<u16>().ok())
            .is_some_and(|n| n > 0)
}
fn identifier(text: &str) -> bool {
    !text.is_empty()
        && text.len() <= 160
        && text
            .bytes()
            .all(|x| x.is_ascii_alphanumeric() || b"._:-".contains(&x))
}
fn application_version(text: &str) -> bool {
    if text.is_empty() || text.len() > 64 {
        return false;
    }
    let (core, tail) = text.split_once(['-', '+']).unwrap_or((text, ""));
    let p: Vec<_> = core.split('.').collect();
    p.len() == 3
        && p.iter()
            .all(|s| !s.is_empty() && s.bytes().all(|x| x.is_ascii_digit()))
        && (core.len() == text.len()
            || (!tail.is_empty()
                && tail
                    .bytes()
                    .all(|x| x.is_ascii_alphanumeric() || b".+-".contains(&x))))
}
pub(crate) fn actual_publisher_id(actual: &PackageIdentity) -> Result<&str, &'static str> {
    let family = actual
        .family
        .as_deref()
        .ok_or("desktop_identity_mismatch")?;
    let suffix = family
        .strip_prefix(&(actual.name.clone() + "_"))
        .ok_or("desktop_identity_mismatch")?;
    if suffix.len() != 13
        || !suffix
            .bytes()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit())
    {
        return Err("desktop_identity_mismatch");
    }
    Ok(suffix)
}
impl EngineWindowsIdentity {
    pub fn validate(&self, actual: &PackageIdentity) -> Result<(), &'static str> {
        let suffix = actual_publisher_id(actual)?;
        if suffix.len() != 13
            || !suffix
                .bytes()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit())
            || self.family != format!("{}_{}", self.name, suffix)
            || self.name.eq_ignore_ascii_case(&actual.name)
            || !(3..=50).contains(&self.name.len())
            || !self
                .name
                .bytes()
                .all(|x| x.is_ascii_alphanumeric() || b".-".contains(&x))
            || self.publisher != actual.publisher
            || self.architecture != "x64"
        {
            return Err("desktop_identity_mismatch");
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
    #[test]
    fn shared_related_cases_preserve_current_and_future_contexts() {
        let cases: serde_json::Value = serde_json::from_str(include_str!(
            "../../../shared/desktop-release/related-servicing-cases.json"
        ))
        .unwrap();
        for case in cases["cases"].as_array().unwrap() {
            let bytes = match case["descriptor"].as_str().unwrap() {
                "qualification-direct.json" => {
                    include_str!("../../../shared/desktop-release/qualification-direct.json")
                }
                "qualification-direct-v2-bridge.json" => include_str!(
                    "../../../shared/desktop-release/qualification-direct-v2-bridge.json"
                ),
                "qualification-direct-v2-main-only.json" => include_str!(
                    "../../../shared/desktop-release/qualification-direct-v2-main-only.json"
                ),
                "qualification-direct-v2-related.json" => include_str!(
                    "../../../shared/desktop-release/qualification-direct-v2-related.json"
                ),
                _ => panic!("unknown shared fixture"),
            };
            let release: DesktopRelease = serde_json::from_str(bytes).unwrap();
            let a = &case["actual"];
            let actual = PackageIdentity {
                name: a["name"].as_str().unwrap().into(),
                publisher: a["publisher"].as_str().unwrap().into(),
                version: a["version"].as_str().unwrap().into(),
                architecture: a["architecture"].as_str().unwrap().into(),
                family: Some(a["family"].as_str().unwrap().into()),
            };
            let running = if case["context"] == "future_target" {
                None
            } else {
                case["expected_application_version"].as_str()
            };
            assert_eq!(
                release.validate_context(&actual, running),
                Ok(()),
                "{}",
                case["case"]
            );
            assert_eq!(
                release.allows_target(case["feed_target_kind"] == "related_bundle"),
                case["expected_error"].is_null(),
                "{}",
                case["case"]
            );
            if case["context"] == "future_target" {
                assert!(release.validate(&actual).is_err());
            }
            if release.servicing.is_some() {
                let mut bad = actual.clone();
                bad.family = Some(format!("{}_zzzzzzzzzzzzz", actual.name));
                if release.engine_identity().is_some() {
                    assert!(release.validate_context(&bad, running).is_err());
                }
            }
        }
    }

    #[test]
    fn closed_versions_reject_cross_version_fields_and_missing_v2_servicing() {
        let v1: serde_json::Value = serde_json::from_str(include_str!(
            "../../../shared/desktop-release/qualification-direct.json"
        ))
        .unwrap();
        let v2: serde_json::Value = serde_json::from_str(include_str!(
            "../../../shared/desktop-release/qualification-direct-v2-bridge.json"
        ))
        .unwrap();
        let mut bad = v1.clone();
        bad["servicing"] = v2["servicing"].clone();
        assert!(serde_json::from_value::<DesktopRelease>(bad).is_err());
        let mut bad = v2.clone();
        bad.as_object_mut().unwrap().remove("servicing");
        assert!(serde_json::from_value::<DesktopRelease>(bad).is_err());
        let mut bad = v2;
        bad["servicing"]["installed_artifact"]["engine"] = serde_json::json!({});
        assert!(serde_json::from_value::<DesktopRelease>(bad).is_err());
    }

    #[test]
    fn registered_other_msix_channel_requires_a_deliberate_transition() {
        let identities = production_package_identities().unwrap();
        assert_eq!(identities.len(), 2);
        let mut release: DesktopRelease = serde_json::from_str(include_str!(
            "../../../shared/desktop-release/qualification-direct.json"
        ))
        .unwrap();
        release.purpose = "production".into();
        for (own, other) in [(0, 1), (1, 0)] {
            release.identity = identities[own].clone();
            let mut queried = Vec::new();
            assert_eq!(
                validate_msix_coexistence(Some(&release), true, |name, publisher| {
                    queried.push((name.to_owned(), publisher.to_owned()));
                    Ok(true)
                }),
                Err("runtime_installation_conflict")
            );
            assert_eq!(
                queried,
                vec![(
                    identities[other].name.clone(),
                    identities[other].publisher.clone()
                )]
            );
            assert!(validate_msix_coexistence(Some(&release), true, |_, _| Ok(false)).is_ok());
        }
        assert_eq!(
            validate_msix_coexistence(None, true, |_, _| Ok(true)),
            Err("runtime_installation_conflict")
        );
        assert!(validate_msix_coexistence(None, true, |_, _| Ok(false)).is_ok());
    }

    #[test]
    fn isolated_roots_do_not_query_customer_installations_and_query_failure_refuses() {
        let release: DesktopRelease = serde_json::from_str(include_str!(
            "../../../shared/desktop-release/qualification-direct.json"
        ))
        .unwrap();
        assert!(
            validate_msix_coexistence(Some(&release), true, |_, _| panic!(
                "isolated qualification queried customer packages"
            ))
            .is_ok()
        );
        assert!(validate_msix_coexistence(None, false, |_, _| panic!(
            "external root queried customer packages"
        ))
        .is_ok());
        assert_eq!(
            validate_msix_coexistence(None, true, |_, _| Err("query_failed")),
            Err("runtime_installation_conflict")
        );
    }

    #[test]
    fn activation_names_bind_root_user_and_delivery_but_not_package_version() {
        let mut release: DesktopRelease = serde_json::from_str(include_str!(
            "../../../shared/desktop-release/qualification-direct.json"
        ))
        .unwrap();
        let root = Path::new("C:/retained/qualification/Direct");
        let names = instance_names(root, b"user-one", Some(&release)).unwrap();
        release.package_version = "1.0.2.0".into();
        assert_eq!(
            names,
            instance_names(root, b"user-one", Some(&release)).unwrap()
        );
        let other_user = instance_names(root, b"user-two", Some(&release)).unwrap();
        assert_eq!(names.root_mutex, other_user.root_mutex);
        assert_ne!(names.plugin_id, other_user.plugin_id);
        let other_root = instance_names(
            Path::new("C:/retained/another"),
            b"user-one",
            Some(&release),
        )
        .unwrap();
        assert_ne!(names.root_mutex, other_root.root_mutex);
        assert_ne!(names.plugin_id, other_root.plugin_id);
        let legacy = instance_names(root, b"user-one", None).unwrap();
        assert_eq!(names.root_mutex, legacy.root_mutex);
        assert_ne!(names.plugin_id, legacy.plugin_id);
        release.identity.publisher = "CN=Another".into();
        assert_ne!(
            names.plugin_id,
            instance_names(root, b"user-one", Some(&release))
                .unwrap()
                .plugin_id
        );
        assert_eq!(
            names,
            instance_names(
                Path::new("c:/RETAINED/qualification/DIRECT"),
                b"user-one",
                Some(
                    &serde_json::from_str(include_str!(
                        "../../../shared/desktop-release/qualification-direct.json"
                    ))
                    .unwrap()
                )
            )
            .unwrap()
        );
    }
    fn actual(name: &str, publisher: &str) -> PackageIdentity {
        PackageIdentity {
            name: name.into(),
            publisher: publisher.into(),
            version: "1.0.1.0".into(),
            architecture: "x64".into(),
            family: None,
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
