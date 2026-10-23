//! Closed related-bundle acquisition probe; debug qualification builds only.
use quick_xml::{
    events::{BytesStart, Event},
    Reader,
};
use serde::{Deserialize, Serialize};
use std::{collections::BTreeMap, path::Path, time::Duration};

const MAX_DESCRIPTOR: usize = 16_384;
const OPERATION_BUDGET: Duration = Duration::from_secs(90);
const NAMESPACE: &str = "http://schemas.microsoft.com/appx/appinstaller/2018";

#[derive(Debug, Serialize)]
pub(crate) struct Failure {
    reason: &'static str,
    hresult: Option<i32>,
}
type Result<T> = std::result::Result<T, Failure>;
fn refused(reason: &'static str) -> Failure {
    Failure {
        reason,
        hresult: None,
    }
}
fn require(value: bool, reason: &'static str) -> Result<()> {
    if value {
        Ok(())
    } else {
        Err(refused(reason))
    }
}
impl From<windows_runtime::core::Error> for Failure {
    fn from(value: windows_runtime::core::Error) -> Self {
        Self {
            reason: "windows_api_failed",
            hresult: Some(value.code().0),
        }
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Profile {
    schema: String,
    purpose: String,
    descriptor_kind: String,
    nonce: String,
    core_name: String,
    core_family: String,
    core_version: String,
    previous_core_version: String,
    engine_name: String,
    engine_family: String,
    engine_version: String,
    publisher: String,
    source_uri: String,
    engine_bundle_uri: String,
    engine_bundle_sha256: String,
    worker_sha256: String,
}

fn hex(value: &str, size: usize) -> bool {
    value.len() == size
        && value
            .bytes()
            .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
}
fn version(value: &str) -> bool {
    let parts: Vec<_> = value.split('.').collect();
    parts.len() == 4
        && parts[0] == "1"
        && parts[3] == "0"
        && parts
            .iter()
            .all(|part| part.parse::<u16>().is_ok_and(|n| n.to_string() == *part))
}
fn version_parts(value: &str) -> Option<Vec<u16>> {
    value
        .split('.')
        .map(str::parse::<u16>)
        .collect::<std::result::Result<Vec<_>, _>>()
        .ok()
}
fn canonical_https(value: &str) -> Result<url::Url> {
    let parsed = url::Url::parse(value).map_err(|_| refused("source_uri_invalid"))?;
    require(
        parsed.scheme() == "https"
            && parsed.host_str().is_some()
            && parsed.port_or_known_default() == Some(443)
            && parsed.username().is_empty()
            && parsed.password().is_none()
            && parsed.query().is_none()
            && parsed.fragment().is_none()
            && parsed.as_str() == value
            && !value.contains('%')
            && !value.contains('\\')
            && !value.chars().any(|c| c <= ' '),
        "source_uri_invalid",
    )?;
    Ok(parsed)
}
impl Profile {
    fn validate(&self) -> Result<()> {
        require(
            self.schema == "text-analysis-related-bundle/v1"
                && self.purpose == "qualification"
                && self.descriptor_kind == "related_set_bundle"
                && hex(&self.nonce, 32),
            "profile_invalid",
        )?;
        for name in [&self.core_name, &self.engine_name] {
            require(
                (3..=50).contains(&name.len())
                    && name.contains(".Qualification.")
                    && name
                        .bytes()
                        .all(|c| c.is_ascii_alphanumeric() || b".-".contains(&c)),
                "profile_identity_invalid",
            )?;
        }
        require(
            self.core_name != self.engine_name
                && self.publisher.starts_with("CN=")
                && self.publisher.len() <= 256
                && !self.publisher.chars().any(|c| c < ' ')
                && version(&self.core_version)
                && version(&self.previous_core_version)
                && version(&self.engine_version)
                && version_parts(&self.previous_core_version) < version_parts(&self.core_version),
            "profile_identity_invalid",
        )?;
        let suffix = self
            .core_family
            .strip_prefix(&(self.core_name.clone() + "_"))
            .ok_or_else(|| refused("profile_family_invalid"))?;
        require(
            suffix.len() == 13
                && suffix
                    .bytes()
                    .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit())
                && self.engine_family == format!("{}_{}", self.engine_name, suffix),
            "profile_family_invalid",
        )?;
        require(
            hex(&self.engine_bundle_sha256, 64) && hex(&self.worker_sha256, 64),
            "profile_digest_invalid",
        )?;
        let source = canonical_https(&self.source_uri)?;
        let base = self
            .source_uri
            .strip_suffix("probe.appinstaller")
            .ok_or_else(|| refused("source_uri_invalid"))?;
        require(
            source.path().contains(&format!("/{}/", self.nonce))
                && self.engine_bundle_uri
                    == format!(
                        "{base}artifacts/{}/engine.msixbundle",
                        self.engine_bundle_sha256
                    ),
            "artifact_uri_invalid",
        )?;
        for value in [&self.engine_bundle_uri] {
            require(
                canonical_https(value)?.origin() == source.origin(),
                "artifact_origin_invalid",
            )?;
        }
        Ok(())
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Inventory {
    nonce: String,
    main_bundle_uri: String,
    main_bundle_sha256: String,
    main_bundle_size: u64,
    descriptor_sha256: String,
}
impl Inventory {
    fn validate(&self, profile: &Profile) -> Result<()> {
        let base = profile
            .source_uri
            .strip_suffix("probe.appinstaller")
            .ok_or_else(|| refused("inventory_invalid"))?;
        require(
            self.nonce == profile.nonce
                && hex(&self.main_bundle_sha256, 64)
                && hex(&self.descriptor_sha256, 64)
                && self.main_bundle_size > 0
                && self.main_bundle_size <= 200 * 1024 * 1024
                && self.main_bundle_uri
                    == format!(
                        "{base}artifacts/{}/main.msixbundle",
                        self.main_bundle_sha256
                    ),
            "inventory_invalid",
        )?;
        canonical_https(&self.main_bundle_uri)?;
        Ok(())
    }
}

fn next<'a>(reader: &mut Reader<&'a [u8]>) -> Result<Event<'a>> {
    loop {
        let event = reader
            .read_event()
            .map_err(|_| refused("descriptor_invalid"))?;
        if matches!(&event, Event::Text(t) if t.as_ref().bytes().all(|c| c.is_ascii_whitespace())) {
            continue;
        }
        return Ok(event);
    }
}
fn attributes(element: &BytesStart<'_>, expected: &[(&str, &str)]) -> Result<()> {
    let mut values = BTreeMap::new();
    for attribute in element.attributes() {
        let attribute = attribute.map_err(|_| refused("descriptor_invalid"))?;
        let key = attribute.key.as_ref().to_owned();
        let value = attribute
            .normalized_value(quick_xml::XmlVersion::Implicit1_0)
            .map_err(|_| refused("descriptor_invalid"))?
            .into_owned();
        require(values.insert(key, value).is_none(), "descriptor_invalid")?;
    }
    require(
        values.len() == expected.len()
            && expected
                .iter()
                .all(|(key, value)| values.get(*key).is_some_and(|actual| actual == value)),
        "descriptor_invalid",
    )
}
fn empty(reader: &mut Reader<&[u8]>, name: &str, attrs: &[(&str, &str)]) -> Result<()> {
    match next(reader)? {
        Event::Empty(element) if element.name().as_ref() == name => attributes(&element, attrs),
        _ => Err(refused("descriptor_invalid")),
    }
}
fn start(reader: &mut Reader<&[u8]>, name: &str, attrs: &[(&str, &str)]) -> Result<()> {
    match next(reader)? {
        Event::Start(element) if element.name().as_ref() == name => attributes(&element, attrs),
        _ => Err(refused("descriptor_invalid")),
    }
}
fn end(reader: &mut Reader<&[u8]>, name: &str) -> Result<()> {
    require(
        matches!(next(reader)?, Event::End(element) if element.name().as_ref() == name),
        "descriptor_invalid",
    )
}
fn parse_descriptor(bytes: &[u8], profile: &Profile, inventory: &Inventory) -> Result<()> {
    profile.validate()?;
    inventory.validate(profile)?;
    require(
        !bytes.is_empty() && bytes.len() <= MAX_DESCRIPTOR,
        "descriptor_size_invalid",
    )?;
    let text = std::str::from_utf8(bytes).map_err(|_| refused("descriptor_invalid"))?;
    let mut reader = Reader::from_str(text.trim_start_matches('\u{feff}'));
    let event = next(&mut reader)?;
    let root = if let Event::Decl(declaration) = event {
        require(
            declaration
                .version()
                .map_err(|_| refused("descriptor_invalid"))?
                .as_ref()
                == "1.0"
                && declaration
                    .encoding()
                    .transpose()
                    .map_err(|_| refused("descriptor_invalid"))?
                    .is_none_or(|v| v.as_ref() == "utf-8" || v.as_ref() == "UTF-8"),
            "descriptor_invalid",
        )?;
        next(&mut reader)?
    } else {
        event
    };
    match root {
        Event::Start(element) if element.name().as_ref() == "AppInstaller" => attributes(
            &element,
            &[
                ("xmlns", NAMESPACE),
                ("Version", &profile.core_version),
                ("Uri", &profile.source_uri),
            ],
        )?,
        _ => return Err(refused("descriptor_invalid")),
    }
    empty(
        &mut reader,
        "MainBundle",
        &[
            ("Name", &profile.core_name),
            ("Publisher", &profile.publisher),
            ("Version", &profile.core_version),
            ("Uri", &inventory.main_bundle_uri),
        ],
    )?;
    start(&mut reader, "RelatedPackages", &[])?;
    empty(
        &mut reader,
        "Bundle",
        &[
            ("Name", &profile.engine_name),
            ("Publisher", &profile.publisher),
            ("Version", &profile.engine_version),
            ("Uri", &profile.engine_bundle_uri),
        ],
    )?;
    end(&mut reader, "RelatedPackages")?;
    start(&mut reader, "UpdateSettings", &[])?;
    empty(
        &mut reader,
        "OnLaunch",
        &[
            ("HoursBetweenUpdateChecks", "6"),
            ("ShowPrompt", "false"),
            ("UpdateBlocksActivation", "false"),
        ],
    )?;
    empty(&mut reader, "AutomaticBackgroundTask", &[])?;
    end(&mut reader, "UpdateSettings")?;
    end(&mut reader, "AppInstaller")?;
    require(
        matches!(next(&mut reader)?, Event::Eof),
        "descriptor_invalid",
    )
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub(crate) enum Action {
    Bootstrap,
    Inspect,
    Acquire,
    CancelAcquire,
}
#[derive(Debug, Serialize)]
struct Caller {
    elevated: bool,
    elevation_type: u32,
    integrity_rid: u32,
    app_container: bool,
    package: Option<String>,
    session_id: u32,
}
fn admit_caller(caller: &Caller, action: Action, expected_package: &str) -> Result<()> {
    require(
        !caller.elevated
            && caller.integrity_rid == 0x2000
            && !caller.app_container
            && caller.session_id != 0,
        "ordinary_medium_caller_required",
    )?;
    require(
        if action == Action::Bootstrap {
            caller.package.is_none()
        } else {
            caller.package.as_deref() == Some(expected_package)
        },
        "caller_package_mismatch",
    )
}
fn timed_out(elapsed: Duration, cancelled: bool) -> bool {
    cancelled || elapsed >= OPERATION_BUDGET
}

#[path = "cancellation_policy.rs"]
mod cancellation_policy;
#[path = "optional_acquisition_windows.rs"]
mod native;
pub(crate) use native::run;

#[cfg(test)]
mod tests {
    use super::*;

    fn profile() -> Profile {
        let nonce = "a".repeat(32);
        let base = format!("https://localhost/{nonce}/");
        Profile {
            schema: "text-analysis-related-bundle/v1".into(),
            purpose: "qualification".into(),
            descriptor_kind: "related_set_bundle".into(),
            nonce,
            core_name: "Text.Qualification.Main".into(),
            core_family: "Text.Qualification.Main_abcdefghijklm".into(),
            core_version: "1.0.3.0".into(),
            previous_core_version: "1.0.2.0".into(),
            engine_name: "Text.Qualification.Engine".into(),
            engine_family: "Text.Qualification.Engine_abcdefghijklm".into(),
            engine_version: "1.0.3.0".into(),
            publisher: "CN=Text Qualification".into(),
            source_uri: format!("{base}probe.appinstaller"),
            engine_bundle_uri: format!("{base}artifacts/{}/engine.msixbundle", "c".repeat(64)),
            engine_bundle_sha256: "c".repeat(64),
            worker_sha256: "d".repeat(64),
        }
    }
    fn inventory(p: &Profile) -> Inventory {
        let base = p.source_uri.strip_suffix("probe.appinstaller").unwrap();
        Inventory {
            nonce: p.nonce.clone(),
            main_bundle_uri: format!("{base}artifacts/{}/main.msixbundle", "b".repeat(64)),
            main_bundle_sha256: "b".repeat(64),
            main_bundle_size: 123,
            descriptor_sha256: "e".repeat(64),
        }
    }
    fn descriptor(p: &Profile) -> String {
        format!(
            r#"<AppInstaller xmlns="{NAMESPACE}" Version="{}" Uri="{}"><MainBundle Name="{}" Publisher="{}" Version="{}" Uri="{}"/><RelatedPackages><Bundle Name="{}" Publisher="{}" Version="{}" Uri="{}"/></RelatedPackages><UpdateSettings><OnLaunch HoursBetweenUpdateChecks="6" ShowPrompt="false" UpdateBlocksActivation="false"/><AutomaticBackgroundTask/></UpdateSettings></AppInstaller>"#,
            p.core_version,
            p.source_uri,
            p.core_name,
            p.publisher,
            p.core_version,
            inventory(p).main_bundle_uri,
            p.engine_name,
            p.publisher,
            p.engine_version,
            p.engine_bundle_uri
        )
    }
    #[test]
    fn related_descriptor_accepts_only_the_closed_profile() {
        let p = profile();
        let valid = descriptor(&p);
        assert!(parse_descriptor(valid.as_bytes(), &p, &inventory(&p)).is_ok());
        for changed in [
            valid.replace(NAMESPACE, "urn:other"),
            valid.replace("MainBundle", "MainPackage"),
            valid.replace("RelatedPackages", "OptionalPackages"),
            valid.replace("Checks=\"6\"", "Checks=\"0\""),
            valid.replace("ShowPrompt=\"false\"", "ShowPrompt=\"true\""),
            valid.replace("<AutomaticBackgroundTask/>", ""),
            valid.replace("</RelatedPackages>", "<Bundle/></RelatedPackages>"),
            valid.replace("<RelatedPackages>", "<!-- unexpected --><RelatedPackages>"),
            valid.replace(
                &p.engine_bundle_uri,
                "https://elsewhere.invalid/engine.msixbundle",
            ),
            format!("{valid}<AppInstaller/>"),
        ] {
            assert!(parse_descriptor(changed.as_bytes(), &p, &inventory(&p)).is_err());
        }
        assert!(parse_descriptor(&vec![b' '; MAX_DESCRIPTOR + 1], &p, &inventory(&p)).is_err());
    }
    #[test]
    fn profile_rejects_source_substitution_and_nonforward_bootstrap() {
        for source in [
            "http://localhost/a/probe.appinstaller",
            "https://localhost:18443/a/probe.appinstaller",
            "https://user@localhost/a/probe.appinstaller",
            "https://localhost/a/probe.appinstaller?q=1",
            "https://localhost/%61/probe.appinstaller",
        ] {
            assert!(canonical_https(source).is_err());
        }
        let mut p = profile();
        p.previous_core_version = p.core_version.clone();
        assert!(p.validate().is_err());
        let mut p = profile();
        p.engine_family.push('x');
        assert!(p.validate().is_err());
        let mut p = profile();
        p.engine_bundle_uri.push_str("?cache=1");
        assert!(p.validate().is_err());
        assert!(!version("1.00.3.0"));
        assert!(!version("1.0.65536.0"));
    }
    #[test]
    fn finished_main_inventory_is_external_and_exact() {
        let p = profile();
        let mut external = inventory(&p);
        assert!(external.validate(&p).is_ok());
        external.main_bundle_sha256 = "f".repeat(64);
        assert!(external.validate(&p).is_err());
        external = inventory(&p);
        external.nonce = "f".repeat(32);
        assert!(external.validate(&p).is_err());
        external = inventory(&p);
        external.main_bundle_size = 200 * 1024 * 1024 + 1;
        assert!(external.validate(&p).is_err());
        external = inventory(&p);
        external.main_bundle_sha256 = "f".repeat(64);
        external.main_bundle_uri = external
            .main_bundle_uri
            .replace(&"b".repeat(64), &"f".repeat(64));
        assert!(external.validate(&p).is_ok());
        assert!(parse_descriptor(descriptor(&p).as_bytes(), &p, &external).is_err());
        let encoded = serde_json::json!({"schema":"text-analysis-related-bundle/v1","main_bundle_sha256":"unexpected"});
        assert!(serde_json::from_value::<Profile>(encoded).is_err());
    }
    #[test]
    fn bootstrap_and_packaged_caller_authorities_are_distinct() {
        let mut caller = Caller {
            elevated: false,
            elevation_type: 3,
            integrity_rid: 0x2000,
            app_container: false,
            package: None,
            session_id: 1,
        };
        assert!(admit_caller(&caller, Action::Bootstrap, "exact").is_ok());
        assert!(admit_caller(&caller, Action::Acquire, "exact").is_err());
        caller.package = Some("exact".into());
        assert!(admit_caller(&caller, Action::Bootstrap, "exact").is_err());
        assert!(admit_caller(&caller, Action::Acquire, "exact").is_ok());
        caller.elevated = true;
        assert!(admit_caller(&caller, Action::Acquire, "exact").is_err());
        caller.elevated = false;
        caller.integrity_rid = 0x3000;
        assert!(admit_caller(&caller, Action::Acquire, "exact").is_err());
    }
    #[test]
    fn deadline_and_cancellation_refuse_at_the_boundary() {
        assert!(!timed_out(
            OPERATION_BUDGET - Duration::from_millis(1),
            false
        ));
        assert!(timed_out(OPERATION_BUDGET, false));
        assert!(timed_out(Duration::ZERO, true));
    }
}
