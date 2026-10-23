//! The one producer's closed descriptor. This module never downloads package bytes.
use crate::deployment::{DesktopRelease, EngineWindowsIdentity, PackageIdentity};
use quick_xml::{
    events::{BytesStart, Event},
    Reader,
};
use std::collections::BTreeMap;

pub const MAX_DESCRIPTOR: usize = 16_384;
#[derive(Clone, Debug, PartialEq)]
pub struct Coordinate {
    pub version: String,
    pub package_uri: String,
}
#[derive(Clone, Debug, PartialEq)]
pub enum Target {
    MainPackage {
        core: Coordinate,
    },
    RelatedBundle {
        core: Coordinate,
        engine_identity: EngineWindowsIdentity,
        engine: Coordinate,
    },
}
impl Target {
    pub fn core(&self) -> &Coordinate {
        match self {
            Self::MainPackage { core } | Self::RelatedBundle { core, .. } => core,
        }
    }
}
type Result<T> = std::result::Result<T, &'static str>;

fn version(text: &str) -> Result<[u16; 4]> {
    let parts: Vec<_> = text.split('.').collect();
    if parts.len() != 4 {
        return Err("update_version_invalid");
    }
    let mut values = [0; 4];
    for (part, slot) in parts.iter().zip(&mut values) {
        *slot = part.parse().map_err(|_| "update_version_invalid")?;
        if slot.to_string() != *part {
            return Err("update_version_invalid");
        }
    }
    if values[0] != 1 || values[3] != 0 {
        return Err("update_version_invalid");
    }
    Ok(values)
}
fn next<'a>(reader: &mut Reader<&'a [u8]>) -> Result<Event<'a>> {
    loop {
        let event = reader
            .read_event()
            .map_err(|_| "update_descriptor_invalid")?;
        if matches!(&event, Event::Text(text) if text.as_ref().bytes().all(|byte| byte.is_ascii_whitespace()))
        {
            continue;
        }
        return Ok(event);
    }
}
fn attributes(element: &BytesStart<'_>, keys: &[&str]) -> Result<BTreeMap<String, String>> {
    let mut values = BTreeMap::new();
    for attribute in element.attributes() {
        let attribute = attribute.map_err(|_| "update_descriptor_invalid")?;
        let key = attribute.key.as_ref();
        if !keys.contains(&key) {
            return Err("update_descriptor_invalid");
        }
        let value = attribute
            .normalized_value(quick_xml::XmlVersion::Implicit1_0)
            .map_err(|_| "update_descriptor_invalid")?;
        if values.insert(key.to_owned(), value.into_owned()).is_some() {
            return Err("update_descriptor_invalid");
        }
    }
    if values.len() != keys.len() {
        return Err("update_descriptor_invalid");
    }
    Ok(values)
}
fn empty(
    reader: &mut Reader<&[u8]>,
    name: &str,
    keys: &[&str],
) -> Result<BTreeMap<String, String>> {
    match next(reader)? {
        Event::Empty(element) if element.name().as_ref() == name => attributes(&element, keys),
        _ => Err("update_descriptor_invalid"),
    }
}
fn end(reader: &mut Reader<&[u8]>, name: &str) -> Result<()> {
    if matches!(next(reader)?, Event::End(element) if element.name().as_ref()==name) {
        Ok(())
    } else {
        Err("update_descriptor_invalid")
    }
}

pub fn parse(bytes: &[u8], release: &DesktopRelease, actual: &PackageIdentity) -> Result<Target> {
    if release.update_owner != "direct-appinstaller" || release.validate(actual).is_err() {
        return Err("direct_source_required");
    }
    if bytes.is_empty() || bytes.len() > MAX_DESCRIPTOR {
        return Err("update_descriptor_size_invalid");
    }
    let text = std::str::from_utf8(bytes).map_err(|_| "update_descriptor_invalid")?;
    let mut reader = Reader::from_str(text.trim_start_matches('\u{feff}'));
    let mut event = next(&mut reader)?;
    if let Event::Decl(declaration) = event {
        if declaration
            .version()
            .map_err(|_| "update_descriptor_invalid")?
            .as_ref()
            != "1.0"
            || declaration
                .encoding()
                .transpose()
                .map_err(|_| "update_descriptor_invalid")?
                .is_some_and(|value| value.as_ref() != "utf-8" && value.as_ref() != "UTF-8")
        {
            return Err("update_descriptor_invalid");
        }
        event = next(&mut reader)?;
    }
    let root = match event {
        Event::Start(element) if element.name().as_ref() == "AppInstaller" => {
            attributes(&element, &["xmlns", "Version", "Uri"])?
        }
        _ => return Err("update_descriptor_invalid"),
    };
    if root["xmlns"] != "http://schemas.microsoft.com/appx/appinstaller/2018"
        || Some(root["Uri"].as_str()) != release.app_installer_uri.as_deref()
    {
        return Err("update_source_mismatch");
    }
    let (related, package) = match next(&mut reader)? {
        Event::Empty(e) if e.name().as_ref() == "MainPackage" => (
            false,
            attributes(
                &e,
                &[
                    "Name",
                    "Publisher",
                    "Version",
                    "ProcessorArchitecture",
                    "Uri",
                ],
            )?,
        ),
        Event::Empty(e) if e.name().as_ref() == "MainBundle" && release.servicing.is_some() => (
            true,
            attributes(&e, &["Name", "Publisher", "Version", "Uri"])?,
        ),
        _ => return Err("update_descriptor_invalid"),
    };
    if !release.allows_target(related) {
        return Err("update_target_layout_not_allowed");
    }
    if package["Name"] != actual.name
        || package["Publisher"] != actual.publisher
        || (!related && package["ProcessorArchitecture"] != "x64")
        || package["Version"] != root["Version"]
    {
        return Err("update_identity_mismatch");
    }
    if version(&package["Version"])? <= version(&actual.version)? {
        return Err("update_not_newer");
    }
    let engine = if related {
        match next(&mut reader)? {
            Event::Start(e)
                if e.name().as_ref() == "RelatedPackages" && e.attributes().next().is_none() =>
            {
                ()
            }
            _ => return Err("update_descriptor_invalid"),
        };
        let e = empty(
            &mut reader,
            "Bundle",
            &["Name", "Publisher", "Version", "Uri"],
        )?;
        end(&mut reader, "RelatedPackages")?;
        let identity = release
            .engine_identity()
            .ok_or("update_target_layout_not_allowed")?;
        if e["Name"] != identity.name || e["Publisher"] != identity.publisher {
            return Err("update_identity_mismatch");
        }
        if !crate::deployment::engine_version(&e["Version"]) {
            return Err("update_version_invalid");
        }
        Some((
            identity.clone(),
            Coordinate {
                version: e["Version"].clone(),
                package_uri: e["Uri"].clone(),
            },
        ))
    } else {
        None
    };
    match next(&mut reader)? {
        Event::Start(element)
            if element.name().as_ref() == "UpdateSettings"
                && element.attributes().next().is_none() => {}
        _ => return Err("update_descriptor_invalid"),
    }
    let on_launch = empty(
        &mut reader,
        "OnLaunch",
        &[
            "HoursBetweenUpdateChecks",
            "ShowPrompt",
            "UpdateBlocksActivation",
        ],
    )?;
    if on_launch["HoursBetweenUpdateChecks"] != "6"
        || on_launch["ShowPrompt"] != "false"
        || on_launch["UpdateBlocksActivation"] != "false"
    {
        return Err("update_policy_mismatch");
    }
    empty(&mut reader, "AutomaticBackgroundTask", &[])?;
    end(&mut reader, "UpdateSettings")?;
    end(&mut reader, "AppInstaller")?;
    if !matches!(next(&mut reader)?, Event::Eof) {
        return Err("update_descriptor_invalid");
    }
    let source = root["Uri"].as_str();
    let uri = package["Uri"].as_str();
    artifact_uri(
        source,
        uri,
        &package["Version"],
        release,
        if related {
            "main_bundle"
        } else {
            "main_package"
        },
    )?;
    if let Some((identity, engine)) = engine {
        artifact_uri(
            source,
            &engine.package_uri,
            &engine.version,
            release,
            "engine",
        )?;
        Ok(Target::RelatedBundle {
            core: Coordinate {
                version: package["Version"].clone(),
                package_uri: uri.into(),
            },
            engine_identity: identity,
            engine,
        })
    } else {
        Ok(Target::MainPackage {
            core: Coordinate {
                version: package["Version"].clone(),
                package_uri: uri.into(),
            },
        })
    }
}

fn artifact_uri(
    source: &str,
    uri: &str,
    version: &str,
    release: &DesktopRelease,
    kind: &str,
) -> Result<()> {
    let approved = if release.purpose == "production" || release.servicing.is_some() {
        source
            .strip_suffix(if release.purpose == "production" {
                "windows/x64/stable/ConversationAnalytics.appinstaller"
            } else {
                "probe.appinstaller"
            })
            .is_some_and(|base| {
                let Some(rest) = uri.strip_prefix(&(base.to_owned() + "packages/")) else {
                    return false;
                };
                let Some((hash, file)) = rest.split_once('/') else {
                    return false;
                };
                hash.len() == 64
                    && hash
                        .bytes()
                        .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
                    && file
                        == match kind {
                            "main_package" => format!("ConversationAnalytics_{version}_x64.msix"),
                            "main_bundle" => {
                                format!("ConversationAnalytics_{version}_x64.msixbundle")
                            }
                            "engine" => format!("TextAnalysisEngine_{version}_x64.msixbundle"),
                            _ => return false,
                        }
            })
    } else {
        source
            .strip_suffix("probe.appinstaller")
            .is_some_and(|base| {
                uri == base.to_owned()
                    + match kind {
                        "main_package" => "probe.msix",
                        "main_bundle" => "probe.msixbundle",
                        "engine" => "engine.msixbundle",
                        _ => return false,
                    }
            })
    };
    let parsed = url::Url::parse(uri).map_err(|_| "update_package_uri_invalid")?;
    let source_url = url::Url::parse(source).map_err(|_| "update_source_mismatch")?;
    if !approved
        || parsed.scheme() != "https"
        || parsed.origin() != source_url.origin()
        || parsed.as_str() != uri
        || parsed.query().is_some()
        || parsed.fragment().is_some()
        || !parsed.username().is_empty()
        || parsed.password().is_some()
        || (release.servicing.is_some()
            && (uri.contains('%')
                || uri.contains('\\')
                || uri.split('/').any(|s| matches!(s, "." | ".."))))
    {
        return Err("update_package_uri_invalid");
    }
    Ok(())
}

#[cfg(windows)]
pub fn read_metadata(source: &str, cancelled: &impl Fn() -> bool) -> Result<Vec<u8>> {
    use std::{
        ptr::{null, null_mut},
        time::{Duration, Instant},
    };
    use windows_sys::Win32::{Foundation::GetLastError, Networking::WinHttp::*};
    struct Handle(*mut std::ffi::c_void);
    impl Handle {
        fn new(value: *mut std::ffi::c_void) -> Result<Self> {
            if value.is_null() {
                Err("update_source_unavailable")
            } else {
                Ok(Self(value))
            }
        }
    }
    impl Drop for Handle {
        fn drop(&mut self) {
            unsafe {
                WinHttpCloseHandle(self.0);
            }
        }
    }
    fn wide(text: &str) -> Vec<u16> {
        text.encode_utf16().chain(Some(0)).collect()
    }
    unsafe fn header(request: *mut std::ffi::c_void, query: u32) -> Result<Option<String>> {
        let mut length = 0;
        WinHttpQueryHeaders(request, query, null(), null_mut(), &mut length, null_mut());
        if GetLastError() == ERROR_WINHTTP_HEADER_NOT_FOUND {
            return Ok(None);
        }
        if length == 0 || length > 2048 || length % 2 != 0 {
            return Err("update_source_headers_invalid");
        }
        let mut value = vec![0u16; length as usize / 2];
        if WinHttpQueryHeaders(
            request,
            query,
            null(),
            value.as_mut_ptr() as _,
            &mut length,
            null_mut(),
        ) == 0
        {
            return Err("update_source_headers_invalid");
        }
        while value.last() == Some(&0) {
            value.pop();
        }
        String::from_utf16(&value)
            .map(Some)
            .map_err(|_| "update_source_headers_invalid")
    }
    let url = url::Url::parse(source).map_err(|_| "update_source_mismatch")?;
    if url.scheme() != "https"
        || url.query().is_some()
        || url.fragment().is_some()
        || !url.username().is_empty()
        || url.password().is_some()
        || url.as_str() != source
    {
        return Err("update_source_mismatch");
    }
    let started = Instant::now();
    let check = || {
        if cancelled() {
            Err("deployment_cancelled")
        } else if started.elapsed() >= Duration::from_secs(20) {
            Err("update_source_timed_out")
        } else {
            Ok(())
        }
    };
    check()?;
    unsafe {
        let session = Handle::new(WinHttpOpen(
            wide("ConversationAnalytics/0.7.5 WinHTTP").as_ptr(),
            WINHTTP_ACCESS_TYPE_AUTOMATIC_PROXY,
            null(),
            null(),
            0,
        ))?;
        if WinHttpSetTimeouts(session.0, 5000, 5000, 5000, 5000) == 0 {
            return Err("update_source_unavailable");
        }
        let connection = Handle::new(WinHttpConnect(
            session.0,
            wide(url.host_str().ok_or("update_source_mismatch")?).as_ptr(),
            url.port_or_known_default()
                .ok_or("update_source_mismatch")?,
            0,
        ))?;
        let request = Handle::new(WinHttpOpenRequest(
            connection.0,
            wide("GET").as_ptr(),
            wide(url.path()).as_ptr(),
            null(),
            null(),
            null(),
            WINHTTP_FLAG_SECURE,
        ))?;
        let redirect: u32 = WINHTTP_OPTION_REDIRECT_POLICY_NEVER;
        if WinHttpSetOption(
            request.0,
            WINHTTP_OPTION_REDIRECT_POLICY,
            &redirect as *const _ as _,
            4,
        ) == 0
        {
            return Err("update_source_unavailable");
        }
        let headers = wide("Accept: application/appinstaller\r\nAccept-Encoding: identity\r\n");
        if WinHttpSendRequest(
            request.0,
            headers.as_ptr(),
            (headers.len() - 1) as u32,
            null(),
            0,
            0,
            0,
        ) == 0
            || WinHttpReceiveResponse(request.0, null_mut()) == 0
        {
            return Err("update_source_unavailable");
        }
        check()?;
        let mut status = 0u32;
        let mut length = 4;
        if WinHttpQueryHeaders(
            request.0,
            WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
            null(),
            &mut status as *mut _ as _,
            &mut length,
            null_mut(),
        ) == 0
            || status != 200
        {
            return Err("update_source_response_invalid");
        }
        let expected = header(request.0, WINHTTP_QUERY_CONTENT_LENGTH)?
            .ok_or("update_source_headers_invalid")?
            .parse::<usize>()
            .map_err(|_| "update_source_headers_invalid")?;
        if expected == 0 || expected > MAX_DESCRIPTOR {
            return Err("update_descriptor_size_invalid");
        }
        if header(request.0, WINHTTP_QUERY_CONTENT_TYPE)?.as_deref()
            != Some("application/appinstaller")
            || header(request.0, WINHTTP_QUERY_CONTENT_ENCODING)?.is_some_and(|v| v != "identity")
        {
            return Err("update_source_headers_invalid");
        }
        let mut bytes = Vec::with_capacity(expected);
        loop {
            check()?;
            let mut chunk = [0u8; 4096];
            let mut count = 0;
            if WinHttpReadData(
                request.0,
                chunk.as_mut_ptr() as _,
                chunk.len() as u32,
                &mut count,
            ) == 0
            {
                return Err("update_source_unavailable");
            }
            if count == 0 {
                break;
            }
            if bytes.len() + count as usize > expected {
                return Err("update_descriptor_size_invalid");
            }
            bytes.extend_from_slice(&chunk[..count as usize]);
        }
        if bytes.len() != expected {
            return Err("update_descriptor_size_invalid");
        }
        Ok(bytes)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn fixture() -> (DesktopRelease, PackageIdentity, String) {
        let mut release: DesktopRelease = serde_json::from_str(include_str!(
            "../../../shared/desktop-release/qualification-direct.json"
        ))
        .unwrap();
        release.app_installer_uri = Some(
            "https://updates.example.test/desktop/v1/qualification/1234/probe.appinstaller".into(),
        );
        let actual = PackageIdentity {
            name: release.identity.name.clone(),
            publisher: release.identity.publisher.clone(),
            version: "1.0.1.0".into(),
            architecture: "x64".into(),
            family: None,
        };
        let xml = format!(
            r#"<?xml version="1.0" encoding="utf-8"?><AppInstaller xmlns="http://schemas.microsoft.com/appx/appinstaller/2018" Version="1.0.2.0" Uri="{}"><MainPackage Name="{}" Publisher="{}" Version="1.0.2.0" ProcessorArchitecture="x64" Uri="https://updates.example.test/desktop/v1/qualification/1234/probe.msix"/><UpdateSettings><OnLaunch HoursBetweenUpdateChecks="6" ShowPrompt="false" UpdateBlocksActivation="false"/><AutomaticBackgroundTask/></UpdateSettings></AppInstaller>"#,
            release.app_installer_uri.as_ref().unwrap(),
            actual.name,
            actual.publisher
        );
        (release, actual, xml)
    }
    #[test]
    fn related_feed_keeps_engine_version_independent_and_closed() {
        let mut release: DesktopRelease = serde_json::from_str(include_str!(
            "../../../shared/desktop-release/qualification-direct-v2-bridge.json"
        ))
        .unwrap();
        release.app_installer_uri =
            Some("https://updates.example.test/qualification/probe.appinstaller".into());
        let actual = PackageIdentity {
            name: release.identity.name.clone(),
            publisher: release.identity.publisher.clone(),
            version: "1.0.10.0".into(),
            architecture: "x64".into(),
            family: Some(format!("{}_0123456789abc", release.identity.name)),
        };
        let engine = release.engine_identity().unwrap();
        let xml = format!(
            r#"<AppInstaller xmlns="http://schemas.microsoft.com/appx/appinstaller/2018" Version="1.0.11.0" Uri="{}"><MainBundle Name="{}" Publisher="{}" Version="1.0.11.0" Uri="https://updates.example.test/qualification/packages/{}/ConversationAnalytics_1.0.11.0_x64.msixbundle"/><RelatedPackages><Bundle Name="{}" Publisher="{}" Version="42.0.2.3" Uri="https://updates.example.test/qualification/packages/{}/TextAnalysisEngine_42.0.2.3_x64.msixbundle"/></RelatedPackages><UpdateSettings><OnLaunch HoursBetweenUpdateChecks="6" ShowPrompt="false" UpdateBlocksActivation="false"/><AutomaticBackgroundTask/></UpdateSettings></AppInstaller>"#,
            release.app_installer_uri.as_ref().unwrap(),
            actual.name,
            actual.publisher,
            "a".repeat(64),
            engine.name,
            engine.publisher,
            "b".repeat(64)
        );
        let target = parse(xml.as_bytes(), &release, &actual).unwrap();
        assert!(matches!(target,Target::RelatedBundle{engine,..} if engine.version=="42.0.2.3"));
        for bad in [
            xml.replace("42.0.2.3", "0.0.2.3"),
            xml.replace("RelatedPackages", "OptionalPackages"),
            xml.replace("TextAnalysisEngine_42.0.2.3", "TextAnalysisEngine_42.0.3.0"),
            xml.replace("/packages/", "/packages/%61/"),
            xml.replace("<Bundle Name=", "<Bundle Extra=\"x\" Name="),
        ] {
            assert!(parse(bad.as_bytes(), &release, &actual).is_err());
        }
        release.servicing.as_mut().unwrap().allowed_targets =
            crate::deployment::AllowedTargets::MainPackageOnly {};
        assert_eq!(
            parse(xml.as_bytes(), &release, &actual),
            Err("update_target_layout_not_allowed")
        );
    }

    #[test]
    fn closed_feed_binds_exact_source_identity_version_and_policy() {
        let (release, actual, xml) = fixture();
        assert_eq!(
            parse(xml.as_bytes(), &release, &actual)
                .unwrap()
                .core()
                .version,
            "1.0.2.0"
        );
        for changed in [
            xml.replace("Version=\"1.0.2.0\"", "Version=\"1.0.1.0\""),
            xml.replace(
                "ProcessorArchitecture=\"x64\"",
                "ProcessorArchitecture=\"arm64\"",
            ),
            xml.replace("ShowPrompt=\"false\"", "ShowPrompt=\"true\""),
            xml.replace(
                "<AutomaticBackgroundTask/>",
                "<ForceUpdateFromAnyVersion>true</ForceUpdateFromAnyVersion>",
            ),
            xml.replace("probe.msix\"", "probe.msix?other\""),
            xml.replace("probe.msix\"", "../probe.msix\""),
            xml.replace("<MainPackage ", "<MainPackage Extra=\"true\" "),
            xml.replace("<MainPackage ", "<MainPackage Name=\"Other\" "),
            xml.replace("</AppInstaller>", "</AppInstaller><Extra/>"),
            xml.replace(
                "<AppInstaller ",
                "<!DOCTYPE test [<!ENTITY x SYSTEM 'file:///private'>]><AppInstaller ",
            ),
        ] {
            assert!(parse(changed.as_bytes(), &release, &actual).is_err());
        }
        assert!(parse(&[b'x'; MAX_DESCRIPTOR + 1], &release, &actual).is_err());
    }

    #[test]
    fn production_feed_requires_immutable_same_origin_package_coordinate() {
        let (mut release, mut actual, xml) = fixture();
        release.purpose = "production".into();
        release.identity.name = "Dipsy.OnlyFansConversationalAnalytics".into();
        release.identity.publisher = "CN=Dipsy, O=Dipsy, L=London, C=GB".into();
        actual.name = release.identity.name.clone();
        actual.publisher = release.identity.publisher.clone();
        let source = "https://updates.example.test/desktop/v1/windows/x64/stable/ConversationAnalytics.appinstaller";
        release.app_installer_uri = Some(source.into());
        let package = format!(
            "https://updates.example.test/desktop/v1/packages/{}/ConversationAnalytics_1.0.2.0_x64.msix",
            "a".repeat(64)
        );
        let xml = xml
            .replace("OFCA.Qualification.Direct", &actual.name)
            .replace("CN=OFCA MSIX Qualification Direct", &actual.publisher)
            .replace(
                "https://updates.example.test/desktop/v1/qualification/1234/probe.appinstaller",
                source,
            )
            .replace(
                "https://updates.example.test/desktop/v1/qualification/1234/probe.msix",
                &package,
            );
        assert_eq!(
            parse(xml.as_bytes(), &release, &actual)
                .unwrap()
                .core()
                .package_uri,
            package
        );
        for changed in [
            package.replace(&"a".repeat(64), &"A".repeat(64)),
            package.replace(&"a".repeat(64), &"a".repeat(63)),
            package.replace("1.0.2.0_x64", "1.0.3.0_x64"),
            package.replace("updates.example.test", "other.example.test"),
            package.replace("/packages/", "/windows/x64/stable/"),
            format!("{package}?download=true"),
            format!("{package}#fragment"),
        ] {
            assert!(parse(
                xml.replace(&package, &changed).as_bytes(),
                &release,
                &actual
            )
            .is_err());
        }
    }

    #[test]
    fn xml_is_bounded_and_versions_are_canonical_forward_only() {
        let (release, actual, xml) = fixture();
        for invalid in [
            "1.0.02.0",
            "1.0.65536.0",
            "2.0.2.0",
            "1.0.2.1",
            "1.0.2",
            "1.0.0.0",
        ] {
            assert!(parse(
                xml.replace("1.0.2.0", invalid).as_bytes(),
                &release,
                &actual
            )
            .is_err());
        }
        for changed in [
            xml.replace("encoding=\"utf-8\"", "encoding=\"utf-16\""),
            xml.replace("<MainPackage", "<!-- extra --><MainPackage"),
            xml.replace("Publisher=\"CN=OFCA", "Publisher=\"&unknown;CN=OFCA"),
            xml.replace(
                "<AutomaticBackgroundTask/>",
                "<AutomaticBackgroundTask/><Dependencies/>",
            ),
        ] {
            assert!(parse(changed.as_bytes(), &release, &actual).is_err());
        }
        let escaped = xml.replace("CN=OFCA", "CN=OF&#67;A");
        assert!(parse(escaped.as_bytes(), &release, &actual).is_ok());
        assert!(parse(&[0xff], &release, &actual).is_err());
    }
}
