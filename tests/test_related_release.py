"""Invented bytes exercise the v2 producer; no signing or deployment."""
import copy
import io
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import xml.etree.ElementTree as ET
import zipfile

import pytest

from tools import engineering_attestation as attest
from tools import msix_packaging as msix
from tools import optional_msix as optional
from test_packaging_policy import _stage_runtime_tree

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.ci_tier("fast")


def profile(*, production=False, related=True):
    name = "production-direct" if production else "qualification-direct"
    value = msix.read_json(ROOT / f"packaging/msix/profiles/{name}.json")
    value["schema"] = "ofca-msix-package-profile/v2"
    value["release_sequence"] = 11
    if not production:
        value["app_installer_uri"] = "https://qualification.example.test/desktop/qualification/" + "a"*32 + "/probe.appinstaller"
    engine = dict(name="Dipsy.TextAnalysisEngine" if production else "OFCA.Qualification.TextEngine",
                  publisher=value["identity"]["publisher"], architecture="x64")
    engine["family"] = engine["name"] + ("_bvmm2ppd090g2" if production else "_0123456789abc")
    layout = (dict(kind="related_bundle",related_set_id="related.11",engine=dict(identity=engine,package_version="1.0.42.0"))
              if related else dict(kind="main_package"))
    value["servicing"] = dict(installed_artifact=layout,
        allowed_targets=dict(kind="main_package_and_related_bundle",engine_identity=engine))
    return msix.validate_profile(value)


def zipped(entries):
    result = io.BytesIO()
    with zipfile.ZipFile(result,"w") as archive:
        for name, body in entries.items():
            archive.writestr(name,body)
    return result.getvalue()


def bundles(tmp_path, selected):
    relation = optional._release_relation(selected)
    ns = "http://schemas.microsoft.com/appx/2016/bundle"
    def metadata(engine):
        root = ET.Element(f"{{{ns}}}Bundle",SchemaVersion="5.0")
        ET.SubElement(root,f"{{{ns}}}Identity",Name=relation.package_name if engine else relation.core_package_name,
            Publisher=relation.publisher,Version=relation.package_version if engine else relation.core_package_version)
        packages = ET.SubElement(root,f"{{{ns}}}Packages")
        ET.SubElement(packages,f"{{{ns}}}Package",Type="application",Architecture="x64",
            Version=relation.package_version if engine else relation.core_package_version,
            FileName=optional.engine_application_filename(relation.package_version) if engine else optional.artifact_filename("main_package",relation.core_package_version))
        if not engine:
            external = ET.SubElement(root,f"{{{ns}}}OptionalBundle",Name=relation.package_name,Publisher=relation.publisher,
                Version=relation.package_version,FileName=optional.artifact_filename("engine",relation.package_version))
            ET.SubElement(external,f"{{{ns}}}Package",Type="application",Architecture="x64",Version=relation.package_version,
                FileName=optional.engine_application_filename(relation.package_version))
        return ET.tostring(root)
    manifest = optional.optional_manifest(SimpleNamespace(**vars(relation)),minimum_os="10.0.26100.0",tested_os="10.0.26340.0")
    engine_app = zipped({"AppxManifest.xml":manifest,optional.WORKER_PATH:b"invented worker bytes"})
    core, engine = tmp_path/"core.msixbundle",tmp_path/"engine.msixbundle"
    core.write_bytes(zipped({"AppxMetadata/AppxBundleManifest.xml":metadata(False),
        optional.artifact_filename("main_package",relation.core_package_version):b"invented core application"}))
    engine.write_bytes(zipped({"AppxMetadata/AppxBundleManifest.xml":metadata(True),
        optional.engine_application_filename(relation.package_version):engine_app}))
    return core, engine


def test_related_producer_keeps_independent_version_filename_hash_and_external_relation(tmp_path):
    p = profile(); core,engine = bundles(tmp_path,p)
    artifacts = optional.release_artifact_set(p,core,engine)
    assert artifacts["core"]["file"] == "ConversationAnalytics_1.0.11.0_x64.msixbundle"
    assert artifacts["engine"]["artifact"]["file"] == "TextAnalysisEngine_1.0.42.0_x64.msixbundle"
    assert artifacts["core"]["sha256"] != artifacts["engine"]["artifact"]["sha256"]
    xml = ET.fromstring(optional.release_appinstaller(p,artifacts)); ns={"a":optional.APPINSTALLER}
    main, child = xml.find("a:MainBundle",ns),xml.find("a:RelatedPackages/a:Bundle",ns)
    assert "/"+artifacts["core"]["sha256"]+"/" in main.get("Uri")
    assert "/"+artifacts["engine"]["artifact"]["sha256"]+"/" in child.get("Uri")
    assert child.get("Version") == "1.0.42.0" and main.get("Version") == "1.0.11.0"


@pytest.mark.parametrize("mutation",["alias","version","publisher","extra","missing","size","hash"])
def test_tagged_artifact_set_refuses_substitution(tmp_path,mutation):
    p=profile(); core,engine=bundles(tmp_path,p); value=optional.release_artifact_set(p,core,engine)
    if mutation=="alias": value["engine"]["artifact"]["file"]="engine.msixbundle"
    elif mutation=="version": value["engine"]["package_version"]="1.0.11.0"
    elif mutation=="publisher": value["engine"]["identity"]=dict(value["engine"]["identity"],publisher="CN=Other")
    elif mutation=="extra": value["fallback"]="main_package"
    elif mutation=="missing": del value["engine"]
    elif mutation=="size": value["core"]["size"]=True
    elif mutation=="hash": value["engine"]["artifact"]["sha256"]="A"*64
    with pytest.raises(ValueError): optional.release_appinstaller(p,value)


def test_v2_refuses_legacy_optional_alias_while_legacy_parser_stays_compatible(tmp_path):
    p=profile(); core,_=bundles(tmp_path,p)
    with zipfile.ZipFile(core) as archive: entries={n:archive.read(n) for n in archive.namelist()}
    name="AppxMetadata/AppxBundleManifest.xml"
    entries[name]=entries[name].replace(b'TextAnalysisEngine_1.0.42.0_x64.msixbundle',b'engine.msixbundle')
    core.write_bytes(zipped(entries))
    optional.verify_related_bundle(core,optional._release_relation(p))
    with pytest.raises(ValueError,match="canonical"): optional.verify_release_bundle(core,p)


def test_main_only_does_not_accept_or_require_engine(tmp_path):
    p=profile(related=False); core=tmp_path/"core.msix";core.write_bytes(b"invented core bytes")
    artifacts=optional.release_artifact_set(p,core)
    assert set(artifacts)=={"kind","core"}
    assert b"RelatedPackages" not in optional.release_appinstaller(p,artifacts)
    with pytest.raises(ValueError): optional.release_artifact_set(p,core,core)


@pytest.mark.parametrize("related",[False,True])
def test_v2_prepare_seal_and_payload_readback_bind_the_same_descriptor(tmp_path,related):
    selected=profile(related=related)
    stage=_stage_runtime_tree(tmp_path)
    (stage/"release-manifest.json").write_bytes(msix.canonical_json(dict(
        schema="ofca-release-manifest/v1",version=selected["application_version"],architecture="x64",source_commit="1"*40)))
    profile_path=tmp_path/"profile.json";profile_path.write_bytes(msix.canonical_json(selected))
    working=tmp_path/"working";msix.prepare_payload(stage,working,profile_path)
    app=working/"payload/app"
    files=[p for p in app.rglob("*") if p.is_file()]
    (app/"sha256sums.txt").write_text("".join(msix.sha256(p)+" *"+p.relative_to(app).as_posix()+"\n"
        for p in sorted(files)),encoding="ascii")
    tool=tmp_path/"invented-tool-metadata";tool.write_bytes(b"invented SDK fingerprint, never executed")
    inventory=msix.seal_payload(working,profile_path,tool)
    descriptor=app/"desktop-release.json"
    assert inventory["schema"]=="ofca-msix-inventory/v2"
    assert inventory["installed_artifact"]==selected["servicing"]["installed_artifact"]
    assert inventory["desktop_descriptor_sha256"]==msix.sha256(descriptor)
    archive=tmp_path/"invented.msix"
    entries={p.relative_to(working/"payload").as_posix():p.read_bytes()
             for p in (working/"payload").rglob("*") if p.is_file()}
    entries.update({"AppxBlockMap.xml":b"invented footprint","[Content_Types].xml":b"invented footprint"})
    archive.write_bytes(zipped(entries))
    assert msix.verify_archive(archive,working/"inventory.json",profile_path)["package_version"]=="1.0.11.0"
    inventory["desktop_descriptor_sha256"]="f"*64
    (working/"inventory.json").write_bytes(msix.canonical_json(inventory))
    with pytest.raises(ValueError,match="descriptor"): msix.verify_archive(archive,working/"inventory.json",profile_path)


def attestation_fixture(related):
    p=profile(production=True,related=related); layout=p["servicing"]["installed_artifact"]
    source=SimpleNamespace(source_commit="a"*40,source_tree="b"*40)
    encode=msix.canonical_json; digest=attest.sha256_bytes; agent="example-1.0.0-chrome.zip"
    core=dict(file=optional.artifact_filename(layout["kind"],"1.0.11.0"),sha256=digest(b"signed core"),size=11)
    signed=dict(kind=layout["kind"],core=core)
    if related:
        signed.update(related_set_id=layout["related_set_id"],engine=dict(**layout["engine"],artifact=dict(
            file="TextAnalysisEngine_1.0.42.0_x64.msixbundle",sha256=digest(b"signed engine"),size=13)))
    unsigned=copy.deepcopy(signed);unsigned["core"]["sha256"]=digest(b"unsigned core")
    if related: unsigned["engine"]["artifact"]["sha256"]=digest(b"unsigned engine")
    outer={agent:b"invented Agent",core["file"]:b"signed core","sha256sums.txt":b"invented digest file",
        "desktop.profile.json":encode(p),"desktop.appinstaller-template.xml":b"invented template",
        "desktop.resolved-direct-inputs.json":b"{}","ConversationAnalytics.appinstaller":optional.release_appinstaller(p,signed)}
    if related: outer[signed["engine"]["artifact"]["file"]]=b"signed engine"
    profile_hash=digest(outer["desktop.profile.json"])
    descriptor=encode(msix.desktop_descriptor(p,profile_hash)); descriptor_hash=digest(descriptor)
    inventory=dict(schema="ofca-msix-inventory/v2",profile_sha256=profile_hash,identity=p["identity"],purpose="production",
        distribution="direct-appinstaller",application_version=p["application_version"],package_version="1.0.11.0",
        runtime_release=dict(source_commit=source.source_commit),linked_agent={},runtime_policy_sha256="c"*64,
        manifest_template_sha256="c"*64,asset_source_sha256="c"*64,makeappx_sha256="c"*64,
        files=[dict(path="app/desktop-release.json",size=len(descriptor),sha256=descriptor_hash)],
        installed_artifact=layout,desktop_descriptor_sha256=descriptor_hash,artifact_set=unsigned)
    outer["desktop.inventory.json"]=encode(inventory)
    build=dict(schema="ofca-msix-build/v2",verification=dict(package_sha256=unsigned["core"]["sha256"]),windows_sdk="10.0.26100.0",
        makeappx_file_version="invented",uap17_schema_sha256="c"*64,producer_source=dict(base_commit=source.source_commit,dirty=False),artifact_set=unsigned)
    outer["desktop.build.json"]=encode(build)
    family=p["identity"]["name"]+"_bvmm2ppd090g2"
    verified=dict(schema="ofca-msix-release-verification/v2",purpose="production",distribution="direct-appinstaller",mode="DirectSigned",
        identity=dict(Name=p["identity"]["name"],Publisher=p["identity"]["publisher"],Version="1.0.11.0",Architecture="x64",FamilyName=family),
        file_count=1,package_sha256=core["sha256"],profile_sha256=profile_hash,inventory_sha256=digest(outer["desktop.inventory.json"]),signature="trusted-timestamped",artifact_set=signed)
    if related:
        e=layout["engine"];verified["engine_identity"]=dict(Name=e["identity"]["name"],Publisher=e["identity"]["publisher"],
            FamilyName=e["identity"]["family"],Architecture="x64",Version=e["package_version"])
    outer["desktop.verification.json"]=encode(verified)
    common=dict(target="msix-direct",source_commit=source.source_commit,source_tree=source.source_tree,package_version="1.0.11.0",
        application_version=p["application_version"],profile_sha256=profile_hash,inventory_sha256=digest(outer["desktop.inventory.json"]),
        build_sha256=digest(outer["desktop.build.json"]),agent_file=agent,agent_sha256=digest(outer[agent]),expected_family=family)
    candidate=dict(common,schema="ofca-msix-release-inputs/v2",direct_delivery_sha256="d"*64,
        appinstaller_template_sha256=digest(outer["desktop.appinstaller-template.xml"]),artifact_set=unsigned)
    final=dict(common,schema="ofca-desktop-artifact-release/v2",verification_sha256=digest(outer["desktop.verification.json"]),
        signature_scope="trusted-timestamped",resolved_direct_inputs_sha256=digest(outer["desktop.resolved-direct-inputs.json"]),
        descriptor_sha256=digest(outer["ConversationAnalytics.appinstaller"]),artifact_set=signed)
    outer["desktop.release-inputs.json"]=encode(candidate);outer["desktop.artifact-release.json"]=encode(final)
    return outer,[agent],source,lambda _:p


@pytest.mark.parametrize("related",[False,True])
def test_attestation_accepts_closed_v2_byte_bindings_without_claiming_trust(related):
    args=attestation_fixture(related)
    assert attest._msix_agent_container(*args)==set(args[0])


@pytest.mark.parametrize("mutation",["engine_bytes","extra_final","old_inventory","extra_verification","descriptor","downgrade"])
def test_attestation_refuses_v2_cross_record_substitutions(mutation):
    outer,agents,source,reader=attestation_fixture(True)
    if mutation=="engine_bytes": outer["TextAnalysisEngine_1.0.42.0_x64.msixbundle"]=b"changed bytes"
    else:
        target="desktop.artifact-release.json"
        if mutation=="old_inventory": target="desktop.inventory.json"
        elif mutation=="extra_verification": target="desktop.verification.json"
        elif mutation=="descriptor": target="desktop.inventory.json"
        value=json.loads(outer[target])
        if mutation=="old_inventory": value["schema"]="ofca-msix-inventory/v1"
        elif mutation=="descriptor": value["desktop_descriptor_sha256"]="e"*64
        elif mutation=="downgrade": value["schema"]="ofca-desktop-artifact-release/v1"
        else: value["fallback"]=True
        outer[target]=msix.canonical_json(value)
        # Rebind outer hashes so refusal exercises the inner closed contract.
        if target!="desktop.artifact-release.json":
            for name in ["desktop.artifact-release.json","desktop.release-inputs.json"]:
                item=json.loads(outer[name]); key="inventory_sha256" if target=="desktop.inventory.json" else "verification_sha256"
                if key in item: item[key]=attest.sha256_bytes(outer[target])
                outer[name]=msix.canonical_json(item)
    with pytest.raises(attest.ContractError): attest._msix_agent_container(outer,agents,source,reader)


def test_powershell_closed_artifact_and_servicing_helpers(tmp_path):
    p=profile();core,engine=bundles(tmp_path,p);artifacts=optional.release_artifact_set(p,core,engine)
    production=profile(production=True)
    template_set=copy.deepcopy(artifacts)
    template_set["engine"]["identity"]=production["servicing"]["installed_artifact"]["engine"]["identity"]
    template_set["core"]["sha256"]="0"*64;template_set["engine"]["artifact"]["sha256"]="0"*64
    template=tmp_path/"template.xml";template.write_bytes(optional.release_appinstaller(production,template_set))
    evidence=tmp_path/"inputs.json";evidence.write_bytes(msix.canonical_json(dict(profile=p,artifacts=artifacts,
        production=production,template_set=template_set,template=str(template),template_sha256=msix.sha256(template))))
    script="""param($Module,$InputPath)
$ErrorActionPreference='Stop'
Import-Module $Module -Force
$v=Read-ReleaseJson $InputPath
$null=Read-DirectAppInstallerTemplate $v.template $v.production '1.0.11.0' $v.template_set.core.file $v.template_sha256 $v.template_set
$v.template_set.engine.artifact.file='engine.msixbundle'
$rejected=$false
try{$null=Read-DirectAppInstallerTemplate $v.template $v.production '1.0.11.0' $v.template_set.core.file $v.template_sha256 $v.template_set}catch{$rejected=$true}
if(-not $rejected){throw 'template alias accepted'}
Assert-DesktopServicing $v.profile.servicing $v.profile.identity ($v.profile.identity.name+'_0123456789abc')
$rows=@(Get-ReleaseArtifactRecords $v.artifacts '1.0.11.0')
if($rows.Count -ne 2){throw 'missing artifact'}
$v.artifacts.engine.artifact.file='engine.msixbundle'
$rejected=$false
try{$null=Get-ReleaseArtifactRecords $v.artifacts '1.0.11.0'}catch{$rejected=$true}
if(-not $rejected){throw 'alias accepted'}
$v.profile.servicing.allowed_targets=@{kind='main_package_only'}
$rejected=$false
try{Assert-DesktopServicing $v.profile.servicing $v.profile.identity}catch{$rejected=$true}
if(-not $rejected){throw 'layout downgrade accepted'}
Write-Output 'PASS'
"""
    path=tmp_path/"check.ps1";path.write_text(script,encoding="utf-8")
    result=subprocess.run(["pwsh","-NoProfile","-NonInteractive","-File",str(path),
        str(ROOT/"packaging/msix/release-artifacts.psm1"),str(evidence)],capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
    assert "PASS" in result.stdout
