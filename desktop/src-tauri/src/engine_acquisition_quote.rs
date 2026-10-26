//! Publisher-expected package facts; Windows transfer bytes are not observed.
use crate::{
    deployment::{DesktopRelease, EngineRelease, InstalledArtifact, PackageIdentity},
    engine_acquisition::Selection,
    servicing, servicing_feed, worker_host,
};
use serde::{Deserialize, Serialize};
use std::path::PathBuf;
type Result<T> = std::result::Result<T, &'static str>;
const MAX_BYTES: usize = 16384;
const MAX_SAFE: u64 = 9_007_199_254_740_991;
#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub(crate) struct Binding {
    pub explicit_selection: Selection,
    pub quote_sha256: String,
    pub review_digest: String,
}
impl Binding {
    pub(crate) fn valid(&self) -> bool {
        worker_host::hex(&self.quote_sha256, 64) && worker_host::hex(&self.review_digest, 64)
    }
    pub(crate) fn valid_for(&self, selection: Selection) -> bool {
        self.valid() && self.explicit_selection == selection
    }
}
#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Bundle {
    file: String,
    uri: String,
    sha256: String,
    size_bytes: u64,
}
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Disk {
    installed_space_budget_bytes: u64,
    temporary_space_budget_bytes: u64,
    rollback_space_budget_bytes: u64,
}
impl Disk {
    fn total(&self) -> Result<u64> {
        let values = [
            self.installed_space_budget_bytes,
            self.temporary_space_budget_bytes,
            self.rollback_space_budget_bytes,
        ];
        if values.iter().any(|v| *v > MAX_SAFE) || values[0] == 0 {
            return Err("optional_quote_invalid");
        }
        values.into_iter().try_fold(0u64, |sum, v| {
            sum.checked_add(v)
                .filter(|s| *s <= MAX_SAFE)
                .ok_or("optional_quote_invalid")
        })
    }
}
#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Quote {
    schema: String,
    desktop_descriptor_sha256: String,
    related_set_id: String,
    engine: EngineRelease,
    bundle: Bundle,
    disk: Disk,
}
#[derive(Debug, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct DeclaredDisk {
    installed_space_budget_bytes: u64,
    temporary_space_budget_bytes: u64,
    rollback_space_budget_bytes: u64,
    required_free_space_bytes: u64,
}
#[derive(Debug, Serialize)]
#[serde(tag = "availability", rename_all = "snake_case")]
pub(crate) enum Review {
    Available {
        explicit_selection: Selection,
        review_digest: String,
        engine_package_version: String,
        expected_complete_package_bytes: u64,
        declared_disk: DeclaredDisk,
    },
    Unavailable {
        reason: &'static str,
    },
}
pub(crate) struct Verified {
    pub binding: Binding,
    pub response: Review,
    _metadata: servicing::AcquisitionMetadata,
}
fn parse(raw: &[u8], descriptor: &[u8], release: &DesktopRelease) -> Result<Quote> {
    if raw.is_empty()
        || raw.len() > MAX_BYTES
        || descriptor.is_empty()
        || descriptor.len() > MAX_BYTES
    {
        return Err("optional_quote_invalid");
    }
    let quote: Quote = worker_host::decode(raw).map_err(|_| "optional_quote_invalid")?;
    let Some(servicing) = release.servicing.as_ref() else {
        return Err("optional_quote_unavailable");
    };
    let InstalledArtifact::RelatedBundle {
        related_set_id,
        engine,
    } = &servicing.installed_artifact
    else {
        return Err("optional_quote_unavailable");
    };
    if quote.schema != "optional-engine-acquisition-quote/v1"
        || quote.desktop_descriptor_sha256 != worker_host::sha(descriptor)
        || quote.related_set_id != *related_set_id
        || quote.engine != *engine
        || release.update_owner != "direct-appinstaller"
        || !worker_host::hex(&quote.bundle.sha256, 64)
        || quote.bundle.size_bytes == 0
        || quote.bundle.size_bytes > MAX_SAFE
    {
        return Err("optional_quote_invalid");
    }
    quote.disk.total()?;
    let file = format!(
        "TextAnalysisEngine_{}_x64.msixbundle",
        engine.package_version
    );
    let source = release
        .app_installer_uri
        .as_deref()
        .ok_or("optional_quote_invalid")?;
    let suffix = if release.purpose == "production" {
        "windows/x64/stable/ConversationAnalytics.appinstaller"
    } else {
        "probe.appinstaller"
    };
    let base = source
        .strip_suffix(suffix)
        .ok_or("optional_quote_invalid")?;
    if quote.bundle.file != file
        || quote.bundle.uri != format!("{base}packages/{}/{file}", quote.bundle.sha256)
    {
        return Err("optional_quote_invalid");
    }
    Ok(quote)
}
fn bind(
    selection: Selection,
    quote: &Quote,
    raw: &[u8],
    descriptor: &[u8],
    release: &DesktopRelease,
    actual: &PackageIdentity,
    full_name: &str,
    target: &servicing_feed::Target,
) -> Result<Binding> {
    let servicing_feed::Target::RelatedBundle {
        core,
        engine_identity,
        engine,
    } = target
    else {
        return Err("optional_quote_stale");
    };
    if engine_identity != &quote.engine.identity
        || engine.version != quote.engine.package_version
        || engine.package_uri != quote.bundle.uri
        || core.version != actual.version
    {
        return Err("optional_quote_stale");
    }
    let digest = serde_json::to_vec(&(
        "optional-engine-review.v2",
        selection,
        (
            worker_host::sha(raw),
            worker_host::sha(descriptor),
            full_name,
            &actual.name,
            &actual.publisher,
            &actual.version,
            &actual.family,
            &release.app_installer_uri,
            &core.package_uri,
            &engine_identity.name,
            &engine_identity.publisher,
            &engine_identity.family,
            &engine_identity.architecture,
            &engine.version,
            &engine.package_uri,
        ),
    ))
    .map_err(|_| "optional_quote_invalid")?;
    Ok(Binding {
        explicit_selection: selection,
        quote_sha256: worker_host::sha(raw),
        review_digest: worker_host::sha(&digest),
    })
}
pub(crate) fn verified(selection: Selection, cancelled: &impl Fn() -> bool) -> Result<Verified> {
    if cancelled() {
        return Err("optional_cancelled");
    }
    let _apartment = servicing::Apartment::new().map_err(|_| "optional_quote_unavailable")?;
    let (package, actual) = servicing::observe().map_err(|_| "optional_quote_unavailable")?;
    let root = PathBuf::from(
        package
            .InstalledPath()
            .map_err(|_| "optional_quote_unavailable")?
            .to_string(),
    );
    let metadata = servicing::acquisition_metadata(&root)?;
    let release: DesktopRelease =
        worker_host::decode(&metadata.descriptor).map_err(|_| "optional_quote_invalid")?;
    if servicing::bind(&release, &actual) != servicing::UpdateSource::DirectAppinstaller {
        return Err("optional_direct_release_required");
    }
    let image = std::env::current_exe()
        .map_err(|_| "optional_quote_invalid")?
        .canonicalize()
        .map_err(|_| "optional_quote_invalid")?;
    if image
        != root
            .join("app/Bridge.exe")
            .canonicalize()
            .map_err(|_| "optional_quote_invalid")?
    {
        return Err("optional_quote_invalid");
    }
    servicing::automatic_policy(&package)?;
    let quote = parse(&metadata.quote, &metadata.descriptor, &release)?;
    let source = actual
        .app_installer_uri
        .as_deref()
        .ok_or("optional_direct_release_required")?;
    let bytes = servicing_feed::read_metadata(source, cancelled)?;
    let target = servicing_feed::parse_current_related(&bytes, &release, &actual.identity)?;
    let full_name = package
        .Id()
        .and_then(|id| id.FullName())
        .map_err(|_| "optional_quote_invalid")?
        .to_string();
    let binding = bind(
        selection,
        &quote,
        &metadata.quote,
        &metadata.descriptor,
        &release,
        &actual.identity,
        &full_name,
        &target,
    )?;
    if cancelled() {
        return Err("optional_cancelled");
    }
    let response = Review::Available {
        explicit_selection: selection,
        review_digest: binding.review_digest.clone(),
        engine_package_version: quote.engine.package_version,
        expected_complete_package_bytes: quote.bundle.size_bytes,
        declared_disk: DeclaredDisk {
            required_free_space_bytes: quote.disk.total()?,
            installed_space_budget_bytes: quote.disk.installed_space_budget_bytes,
            temporary_space_budget_bytes: quote.disk.temporary_space_budget_bytes,
            rollback_space_budget_bytes: quote.disk.rollback_space_budget_bytes,
        },
    };
    Ok(Verified {
        binding,
        response,
        _metadata: metadata,
    })
}
pub(crate) fn review(selection: Selection, cancelled: &impl Fn() -> bool) -> Result<Review> {
    match verified(selection, cancelled) {
        Ok(v) => Ok(v.response),
        Err(
            reason @ ("optional_quote_unavailable"
            | "optional_quote_invalid"
            | "optional_direct_release_required"),
        ) => Ok(Review::Unavailable { reason }),
        Err(error) => Err(error),
    }
}
pub(crate) fn recheck(expected: &Binding, cancelled: &impl Fn() -> bool) -> Result<Verified> {
    if !expected.valid() {
        return Err("optional_quote_stale");
    }
    let current = verified(expected.explicit_selection, cancelled)?;
    if current.binding != *expected {
        return Err("optional_quote_stale");
    }
    Ok(current)
}

#[cfg(test)]
mod tests {
    use super::*;
    const RAW: &[u8] =
        include_bytes!("../../../packaging/msix/fixtures/engine-acquisition-quote.v1.json");
    const DESCRIPTOR: &[u8] = include_bytes!(
        "../../../packaging/msix/fixtures/engine-acquisition-quote-descriptor.v2.json"
    );
    fn release() -> DesktopRelease {
        worker_host::decode(DESCRIPTOR).unwrap()
    }
    fn fixture() -> (Quote, PackageIdentity, servicing_feed::Target) {
        let r = release();
        let q = parse(RAW, DESCRIPTOR, &r).unwrap();
        let actual = PackageIdentity {
            name: r.identity.name,
            publisher: r.identity.publisher,
            version: "1.0.11.0".into(),
            architecture: "x64".into(),
            family: Some("OFCA.Qualification.Direct_0123456789abc".into()),
        };
        let target=servicing_feed::Target::RelatedBundle{
            core:servicing_feed::Coordinate{version:actual.version.clone(),package_uri:"https://qualification.example.test/desktop/qualification/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/packages/cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc/ConversationAnalytics_1.0.11.0_x64.msixbundle".into()},
            engine_identity:q.engine.identity.clone(),engine:servicing_feed::Coordinate{version:q.engine.package_version.clone(),package_uri:q.bundle.uri.clone()}};
        (q, actual, target)
    }
    #[test]
    fn shared_quote_bytes_and_declared_costs_match_producer_fixture() {
        assert_eq!(
            worker_host::sha(RAW),
            "ea2a9422de4c645a5dc215a23bb6d4785df8503ed450d1883a62936d86ca3a61"
        );
        assert_eq!(
            worker_host::sha(DESCRIPTOR),
            "2cb670d297642ff35a4244ebb9b4d0124930c018283a625bd0a3e059f9be2647"
        );
        let (quote, actual, target) = fixture();
        assert_eq!(quote.disk.total().unwrap(), 6144);
        assert_eq!(quote.bundle.size_bytes, 12345);
        let binding = bind(
            Selection::Module,
            &quote,
            RAW,
            DESCRIPTOR,
            &release(),
            &actual,
            "Core_1.0.11.0_x64__pub",
            &target,
        )
        .unwrap();
        assert!(binding.valid());
    }
    #[test]
    fn quote_refuses_unknown_duplicate_missing_and_noninteger_fields() {
        let value: serde_json::Value = serde_json::from_slice(RAW).unwrap();
        for (field, bad) in [
            ("size_bytes", serde_json::json!(true)),
            ("size_bytes", serde_json::json!(1.5)),
            ("size_bytes", serde_json::json!(0)),
            ("size_bytes", serde_json::json!(MAX_SAFE + 1)),
            (
                "uri",
                serde_json::json!("https://other.test/engine.msixbundle"),
            ),
        ] {
            let mut v = value.clone();
            v["bundle"][field] = bad;
            assert!(parse(&serde_json::to_vec(&v).unwrap(), DESCRIPTOR, &release()).is_err());
        }
        for path in ["bundle", "disk", "engine"] {
            let mut v = value.clone();
            v[path]["extra"] = true.into();
            assert!(parse(&serde_json::to_vec(&v).unwrap(), DESCRIPTOR, &release()).is_err());
        }
        let mut v = value.clone();
        v["disk"]
            .as_object_mut()
            .unwrap()
            .remove("rollback_space_budget_bytes");
        assert!(parse(&serde_json::to_vec(&v).unwrap(), DESCRIPTOR, &release()).is_err());
        let duplicate = String::from_utf8(RAW.to_vec()).unwrap().replacen(
            "{",
            "{\"schema\":\"optional-engine-acquisition-quote/v1\",",
            1,
        );
        assert!(parse(duplicate.as_bytes(), DESCRIPTOR, &release()).is_err());
        assert!(parse(&vec![b' '; MAX_BYTES + 1], DESCRIPTOR, &release()).is_err());
    }
    #[test]
    fn resource_and_descriptor_relation_cannot_be_substituted() {
        let value: serde_json::Value = serde_json::from_slice(RAW).unwrap();
        for (field, bad) in [
            ("installed_space_budget_bytes", serde_json::json!(0)),
            ("temporary_space_budget_bytes", serde_json::json!(-1)),
            ("rollback_space_budget_bytes", serde_json::json!(MAX_SAFE)),
        ] {
            let mut v = value.clone();
            v["disk"][field] = bad;
            assert!(parse(&serde_json::to_vec(&v).unwrap(), DESCRIPTOR, &release()).is_err());
        }
        let mut v = value.clone();
        v["desktop_descriptor_sha256"] = "f".repeat(64).into();
        assert!(parse(&serde_json::to_vec(&v).unwrap(), DESCRIPTOR, &release()).is_err());
        let mut v = value;
        v["engine"]["package_version"] = "1.0.43.0".into();
        assert!(parse(&serde_json::to_vec(&v).unwrap(), DESCRIPTOR, &release()).is_err());
    }
    #[test]
    fn exact_fresh_source_coordinates_and_release_bind_review() {
        let (q, actual, target) = fixture();
        let original = bind(
            Selection::Module,
            &q,
            RAW,
            DESCRIPTOR,
            &release(),
            &actual,
            "CoreA",
            &target,
        )
        .unwrap();
        let mut other = target.clone();
        if let servicing_feed::Target::RelatedBundle { engine, .. } = &mut other {
            engine.package_uri = engine.package_uri.replace(&"b".repeat(64), &"d".repeat(64));
        }
        assert!(bind(
            Selection::Module,
            &q,
            RAW,
            DESCRIPTOR,
            &release(),
            &actual,
            "CoreA",
            &other
        )
        .is_err());
        let mut other = target.clone();
        if let servicing_feed::Target::RelatedBundle { engine, .. } = &mut other {
            engine.version = "1.0.43.0".into();
        }
        assert!(bind(
            Selection::Module,
            &q,
            RAW,
            DESCRIPTOR,
            &release(),
            &actual,
            "CoreA",
            &other
        )
        .is_err());
        assert_ne!(
            original,
            bind(
                Selection::Module,
                &q,
                RAW,
                DESCRIPTOR,
                &release(),
                &actual,
                "CoreB",
                &target
            )
            .unwrap()
        );
        let mut other = target;
        if let servicing_feed::Target::RelatedBundle { core, .. } = &mut other {
            core.package_uri = core.package_uri.replace(&"c".repeat(64), &"e".repeat(64));
        }
        assert_ne!(
            original,
            bind(
                Selection::Module,
                &q,
                RAW,
                DESCRIPTOR,
                &release(),
                &actual,
                "CoreA",
                &other
            )
            .unwrap()
        );
    }
    #[test]
    fn explicit_selection_changes_review_and_cannot_be_omitted_or_substituted() {
        let (q, actual, target) = fixture();
        let module = bind(
            Selection::Module,
            &q,
            RAW,
            DESCRIPTOR,
            &release(),
            &actual,
            "CoreA",
            &target,
        )
        .unwrap();
        let preview = bind(
            Selection::Preview,
            &q,
            RAW,
            DESCRIPTOR,
            &release(),
            &actual,
            "CoreA",
            &target,
        )
        .unwrap();
        assert_eq!(module.quote_sha256, preview.quote_sha256);
        assert_ne!(module.review_digest, preview.review_digest);
        assert!(module.valid_for(Selection::Module));
        assert!(!module.valid_for(Selection::Preview));
        assert!(preview.valid_for(Selection::Preview));
        assert!(!preview.valid_for(Selection::Module));
        let mut value = serde_json::to_value(&preview).unwrap();
        value.as_object_mut().unwrap().remove("explicit_selection");
        assert!(worker_host::decode::<Binding>(&serde_json::to_vec(&value).unwrap()).is_err());
        for selection in [serde_json::Value::Null, serde_json::json!("other")] {
            let mut value = serde_json::to_value(&preview).unwrap();
            value["explicit_selection"] = selection;
            assert!(worker_host::decode::<Binding>(&serde_json::to_vec(&value).unwrap()).is_err());
        }
        let duplicate = String::from_utf8(serde_json::to_vec(&preview).unwrap())
            .unwrap()
            .replacen("{", "{\"explicit_selection\":\"module\",", 1);
        assert!(worker_host::decode::<Binding>(duplicate.as_bytes()).is_err());
    }
    #[test]
    fn no_quote_and_unsupported_release_never_create_an_offer() {
        assert!(parse(&[], DESCRIPTOR, &release()).is_err());
        let mut r = release();
        r.servicing = None;
        assert!(parse(RAW, DESCRIPTOR, &r).is_err());
        let value = serde_json::to_value(Review::Unavailable {
            reason: "optional_quote_unavailable",
        })
        .unwrap();
        assert_eq!(
            value,
            serde_json::json!({"availability":"unavailable","reason":"optional_quote_unavailable"})
        );
    }
}
