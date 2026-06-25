"""Game-aware workshop content analysis helpers."""

import hashlib
import json
import re
import shutil
import struct
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .common import ensure_directory, utc_now_iso


EXECUTABLE_EXTENSIONS = {
    ".bat",
    ".cmd",
    ".com",
    ".dll",
    ".exe",
    ".jar",
    ".js",
    ".lnk",
    ".msi",
    ".ps1",
    ".scr",
    ".vbs",
}

SCRIPT_EXTENSIONS = {
    ".as",
    ".cs",
    ".css",
    ".js",
    ".lua",
    ".nut",
    ".ps1",
    ".py",
    ".sh",
    ".ts",
    ".vbs",
    ".vjs",
    ".vts",
}

SOURCE2_PROGRAMMATIC_EXTENSIONS = {
    ".cfg",
    ".json",
    ".kv3",
    ".res",
    ".txt",
    ".vcss",
    ".vdata",
    ".vdf",
    ".vjs",
    ".vmap",
    ".vmat",
    ".vpulse",
    ".vsmart",
    ".vsndevts",
    ".vsndstck",
    ".vts",
    ".vxml",
    ".xml",
}

UNREAL_PROGRAMMATIC_EXTENSIONS = {
    ".cfg",
    ".conf",
    ".csv",
    ".ini",
    ".int",
    ".json",
    ".locres",
    ".locmeta",
    ".modules",
    ".paklist",
    ".po",
    ".target",
    ".txt",
    ".uproject",
    ".uplugin",
    ".usmap",
    ".utxt",
    ".xml",
}

UNREAL_ASSET_EXTENSIONS = {
    ".uasset",
    ".ubulk",
    ".ucas",
    ".uexp",
    ".umap",
    ".utoc",
}

UNREAL_COOKED_PACKAGE_EXTENSIONS = {".uasset", ".uexp", ".ubulk", ".umap"}

UNREAL_SCRIPT_SCAN_EXTENSIONS = SCRIPT_EXTENSIONS | UNREAL_PROGRAMMATIC_EXTENSIONS

UNREAL_BLUEPRINT_HINTS = {
    "animblueprint",
    "animationblueprint",
    "blueprint",
    "bp_",
    "bpc_",
    "bpi_",
    "bpl_",
    "widgetblueprint",
    "wBP_".lower(),
}

UNREAL_DATA_LOGIC_HINTS = {
    "behavior",
    "bt_",
    "dataasset",
    "datatable",
    "dt_",
    "niagara",
    "pcg",
    "state",
    "widget",
}

PACKAGE_EXTENSIONS = {".pak", ".utoc", ".ucas", ".vpk", ".zip"}

LOW_SIGNAL_ASSET_EXTENSIONS = {
    ".ani",
    ".bmp",
    ".gif",
    ".jpg",
    ".jpeg",
    ".mp3",
    ".ogg",
    ".png",
    ".tga",
    ".vmdl",
    ".vmesh",
    ".vpcf",
    ".vphys",
    ".vsnd",
    ".vtex",
    ".wav",
    ".webp",
}

INTERESTING_NAMES = {
    "assetregistry.bin",
    "addoninfo.txt",
    "gameinfo.gi",
    "manifest.vdf",
    "publish_data.txt",
}


@dataclass
class AnalysisObservation:
    path: str
    source: str
    severity: int
    severity_label: str
    reason: str
    size_bytes: int = 0
    container: str = ""
    category: str = "file"
    include_in_auto: bool = False

    def to_dict(self):
        return {
            "Category": self.category,
            "Path": self.path,
            "Source": self.source,
            "Container": self.container,
            "Severity": self.severity,
            "SeverityLabel": self.severity_label,
            "Reason": self.reason,
            "SizeBytes": self.size_bytes,
            "IncludeInAutomatic": self.include_in_auto,
        }


@dataclass
class AnalysisEvent:
    level: str
    event_type: str
    message: str
    path: str = ""
    source: str = ""
    container: str = ""
    severity: int = 0

    def to_dict(self):
        return {
            "Level": self.level,
            "Type": self.event_type,
            "Message": self.message,
            "Path": self.path,
            "Source": self.source,
            "Container": self.container,
            "Severity": self.severity,
            "SeverityLabel": severity_label(self.severity) if self.severity else "info",
        }


def relative_label(root, path):
    try:
        return str(Path(path).relative_to(Path(root)))
    except ValueError:
        return str(path)


def read_c_string(data, offset):
    end = data.find(b"\x00", offset)
    if end < 0:
        raise ValueError("Unterminated VPK string table entry.")
    return data[offset:end].decode("utf-8", errors="replace"), end + 1


def parse_vpk_directory(path):
    """Return file names from a Valve VPK directory file.

    This intentionally reads only the VPK directory tree. It does not extract
    payload bytes from archive parts, but it gives analysis a parsable view of
    package contents without requiring external tooling.
    """

    path = Path(path)
    data = path.read_bytes()
    if len(data) < 12:
        raise ValueError("VPK file is too small to contain a directory header.")

    signature, version, tree_size = struct.unpack_from("<III", data, 0)
    if signature != 0x55AA1234:
        raise ValueError("Not a VPK directory file.")
    if version == 1:
        offset = 12
    elif version == 2:
        offset = 28
    else:
        raise ValueError("Unsupported VPK version {0}.".format(version))

    tree_end = offset + tree_size
    if tree_end > len(data):
        tree_end = len(data)

    entries = []
    while offset < tree_end:
        extension, offset = read_c_string(data, offset)
        if not extension:
            break

        while offset < tree_end:
            directory, offset = read_c_string(data, offset)
            if not directory:
                break

            while offset < tree_end:
                filename, offset = read_c_string(data, offset)
                if not filename:
                    break
                if offset + 18 > len(data):
                    raise ValueError("VPK directory entry is truncated.")

                _crc, preload_bytes, _archive_index, _entry_offset, entry_length, terminator = (
                    struct.unpack_from("<IHHIIH", data, offset)
                )
                offset += 18
                if terminator != 0xFFFF:
                    raise ValueError("VPK directory entry terminator is invalid.")
                offset += preload_bytes

                leaf = filename if extension == " " else "{0}.{1}".format(filename, extension)
                if directory in ("", " "):
                    full_path = leaf
                else:
                    full_path = "{0}/{1}".format(directory.replace("\\", "/"), leaf)
                entries.append({"Path": full_path, "SizeBytes": entry_length})

    return entries


class WorkshopAnalyzer:
    def __init__(self, state_root, tool_config=None, debug=False):
        self.state_root = Path(state_root)
        self.tool_config = tool_config or {}
        self.debug = debug

    def analysis_root(self, game, workshop_item):
        return (
            self.state_root
            / "analysis"
            / str(game.get("AppId"))
            / str(workshop_item.get("ContentId"))
        )

    def analyze(self, game, workshop_item, content_path, mode="auto"):
        mode = normalize_mode(mode)
        game_type = game.get("GameTypeId")
        if game_type == "source2":
            return self.analyze_source2(game, workshop_item, content_path, mode)
        if game_type == "unreal5":
            return self.analyze_unreal5(game, workshop_item, content_path, mode)
        return self.analyze_unsupported_stub(game, workshop_item, content_path, mode)

    def analyze_source2(self, game, workshop_item, content_path, mode="auto"):
        content_path = Path(content_path)
        output_root = self.analysis_root(game, workshop_item)
        expanded_root = output_root / "expanded"
        if output_root.exists():
            shutil.rmtree(output_root)
        ensure_directory(expanded_root)

        report = self.new_report(game, workshop_item, content_path, output_root, "source2", mode)
        report["Events"].append(
            AnalysisEvent(
                "info",
                "analysis_started",
                "Started Source 2 analysis.",
                source="analysis",
            ).to_dict()
        )
        observations = []
        observations.extend(self.collect_filesystem_observations(content_path, report, "source2"))
        observations.extend(self.expand_zip_archives(content_path, expanded_root, report, "source2"))
        observations.extend(self.collect_vpk_observations(content_path, report))
        report["Observations"] = [observation.to_dict() for observation in sort_findings(observations)]

        self.finalize_report(report)
        self.write_report(output_root, report)
        self.write_content_marker(content_path, report)
        return curate_report(report, mode)

    def analyze_unreal5(self, game, workshop_item, content_path, mode="auto"):
        content_path = Path(content_path)
        output_root = self.analysis_root(game, workshop_item)
        expanded_root = output_root / "expanded"
        extracted_root = output_root / "extracted"
        if output_root.exists():
            shutil.rmtree(output_root)
        ensure_directory(expanded_root)
        ensure_directory(extracted_root)

        report = self.new_report(game, workshop_item, content_path, output_root, "unreal5", mode)
        report["Events"].append(
            AnalysisEvent(
                "info",
                "analysis_started",
                "Started Unreal Engine 5 analysis.",
                source="analysis",
            ).to_dict()
        )
        observations = []
        observations.extend(self.collect_filesystem_observations(content_path, report, "unreal5"))
        observations.extend(self.expand_zip_archives(content_path, expanded_root, report, "unreal5"))
        observations.extend(self.collect_unreal_container_observations(content_path, report, extracted_root))
        observations.extend(self.collect_extracted_file_observations(extracted_root, report, "unreal5"))
        self.scan_unreal_script_files(content_path, extracted_root, report)
        self.parse_unreal_asset_registry(content_path, extracted_root, report)
        self.analyze_unreal_cooked_assets(content_path, extracted_root, report)
        report["Observations"] = [observation.to_dict() for observation in sort_findings(observations)]

        self.finalize_report(report)
        self.write_report(output_root, report)
        self.write_content_marker(content_path, report)
        return curate_report(report, mode)

    def analyze_unsupported_stub(self, game, workshop_item, content_path, mode="auto"):
        content_path = Path(content_path)
        output_root = self.analysis_root(game, workshop_item)
        if output_root.exists():
            shutil.rmtree(output_root)
        ensure_directory(output_root)

        game_type = game.get("GameTypeId") or "unknown"
        report = self.new_report(game, workshop_item, content_path, output_root, game_type, mode)
        report["Events"].append(
            AnalysisEvent(
                "error",
                "unsupported_game_type",
                "Analysis is not implemented for game type '{0}'.".format(game_type),
                source="analysis",
                severity=80,
            ).to_dict()
        )
        self.finalize_report(report)
        self.write_report(output_root, report)
        self.write_content_marker(content_path, report)
        return curate_report(report, mode)

    @staticmethod
    def new_report(game, workshop_item, content_path, output_root, game_type_id, mode):
        return {
            "SchemaVersion": 1,
            "GameTypeId": game_type_id,
            "RequestedMode": mode,
            "GeneratedUtc": utc_now_iso(),
            "Game": {
                "Title": game.get("Title"),
                "AppId": game.get("AppId"),
            },
            "WorkshopItem": {
                "Title": workshop_item.get("Title"),
                "ContentId": workshop_item.get("ContentId"),
            },
            "ContentPath": str(content_path),
            "OutputRoot": str(output_root),
            "ReportPath": str(Path(output_root) / "analysis.json"),
            "Tools": {},
            "Containers": [],
            "ContainerEntries": [],
            "ExtractedFiles": [],
            "ScriptFindings": [],
            "AssetRegistry": {
                "Files": [],
                "Assets": [],
                "Warnings": [],
            },
            "BlueprintFindings": [],
            "DataLogicFindings": [],
            "BlockedItems": [],
            "Observations": [],
            "Events": [],
            "GeneratedFiles": [],
            "ToolInvocations": [],
            "Summary": {},
        }

    @staticmethod
    def finalize_report(report):
        events = report.get("Events", [])
        observations = report.get("Observations", [])
        report["Summary"] = {
            "ObservationCount": len(observations),
            "EventCount": len(events),
            "ErrorCount": sum(1 for event in events if event.get("Level") == "error"),
            "WarningCount": sum(1 for event in events if event.get("Level") == "warning"),
            "GeneratedFileCount": len(report.get("GeneratedFiles", [])),
            "ToolInvocationCount": len(report.get("ToolInvocations", [])),
            "ContainerCount": len(report.get("Containers", [])),
            "ContainerEntryCount": len(report.get("ContainerEntries", [])),
            "ExtractedFileCount": len(report.get("ExtractedFiles", [])),
            "ScriptFindingCount": len(report.get("ScriptFindings", [])),
            "BlueprintFindingCount": len(report.get("BlueprintFindings", [])),
            "DataLogicFindingCount": len(report.get("DataLogicFindings", [])),
            "BlockedItemCount": len(report.get("BlockedItems", [])),
        }

    @staticmethod
    def write_report(output_root, report):
        ensure_directory(output_root)
        report_path = Path(output_root) / "analysis.json"
        report["ReportPath"] = str(report_path)
        if str(report_path) not in report["GeneratedFiles"]:
            report["GeneratedFiles"].append(str(report_path))
        report.setdefault("Summary", {})["GeneratedFileCount"] = len(report["GeneratedFiles"])
        report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    def collect_filesystem_observations(self, content_path, report, game_type_id):
        observations = []
        for path in Path(content_path).rglob("*"):
            if not path.is_file():
                continue
            if ".workshop_analysis" in path.parts:
                continue
            label = relative_label(content_path, path)
            try:
                size = path.stat().st_size
            except OSError as exc:
                size = 0
                report["Events"].append(
                    AnalysisEvent(
                        "error",
                        "filesystem_stat_error",
                        str(exc),
                        path=label,
                        source="filesystem",
                        severity=60,
                    ).to_dict()
                )
            observations.append(classify_path(label, "filesystem", size_bytes=size, game_type_id=game_type_id))
        return observations

    def expand_zip_archives(self, content_path, expanded_root, report, game_type_id):
        observations = []
        for archive_path in Path(content_path).rglob("*.zip"):
            if ".workshop_analysis" in archive_path.parts:
                continue
            archive_label = relative_label(content_path, archive_path)
            target_root = ensure_directory(expanded_root / safe_path_fragment(archive_label))
            try:
                with zipfile.ZipFile(archive_path) as archive:
                    for member in archive.infolist():
                        if member.is_dir():
                            continue
                        member_path = Path(member.filename)
                        if member_path.is_absolute() or ".." in member_path.parts:
                            report["Events"].append(
                                AnalysisEvent(
                                    "error",
                                    "archive_path_traversal",
                                    "Skipped unsafe archive member path.",
                                    path=str(member.filename),
                                    source="zip",
                                    container=archive_label,
                                    severity=90,
                                ).to_dict()
                            )
                            continue
                        try:
                            archive.extract(member, target_root)
                            extracted_path = target_root / member.filename
                            report["GeneratedFiles"].append(str(extracted_path))
                            observations.append(
                                classify_path(
                                    str(member_path).replace("\\", "/"),
                                    "zip",
                                    size_bytes=member.file_size,
                                    container=archive_label,
                                    game_type_id=game_type_id,
                                )
                            )
                        except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                            report["Events"].append(
                                AnalysisEvent(
                                    "error",
                                    "archive_member_extract_error",
                                    str(exc),
                                    path=str(member.filename),
                                    source="zip",
                                    container=archive_label,
                                    severity=90,
                                ).to_dict()
                            )
            except zipfile.BadZipFile as exc:
                report["Events"].append(
                    AnalysisEvent(
                        "error",
                        "archive_corrupt",
                        str(exc),
                        path=archive_label,
                        source="zip",
                        severity=95,
                    ).to_dict()
                )
            except OSError as exc:
                report["Events"].append(
                    AnalysisEvent(
                        "error",
                        "archive_read_error",
                        str(exc),
                        path=archive_label,
                        source="zip",
                        severity=90,
                    ).to_dict()
                )
                continue
        return observations

    def collect_vpk_observations(self, content_path, report):
        observations = []
        for vpk_path in Path(content_path).rglob("*.vpk"):
            if ".workshop_analysis" in vpk_path.parts:
                continue
            archive_label = relative_label(content_path, vpk_path)
            try:
                entries = parse_vpk_directory(vpk_path)
            except (OSError, ValueError, struct.error) as exc:
                report["Events"].append(
                    AnalysisEvent(
                        "error",
                        "vpk_parse_error",
                        str(exc),
                        path=archive_label,
                        source="vpk",
                        severity=95,
                    ).to_dict()
                )
                continue
            for entry in entries:
                observations.append(
                    classify_path(
                        entry["Path"],
                        "vpk",
                        size_bytes=entry.get("SizeBytes", 0),
                        container=archive_label,
                    )
                )
        return observations

    def collect_unreal_container_observations(self, content_path, report, extracted_root):
        report["Tools"] = self.unreal_tool_status()
        observations = []
        observations.extend(self.collect_unreal_pak_observations(content_path, report, extracted_root))
        observations.extend(self.collect_unreal_iostore_observations(content_path, report, extracted_root))
        return observations

    def collect_unreal_pak_observations(self, content_path, report, extracted_root):
        observations = []
        unreal_pak = self.unreal_tool_path("UnrealPakPath")
        for pak_path in Path(content_path).rglob("*.pak"):
            if ".workshop_analysis" in pak_path.parts:
                continue
            pak_label = relative_label(content_path, pak_path)
            container_record = self.new_container_record(pak_label, "pak", pak_path)
            report.setdefault("Containers", []).append(container_record)
            if not unreal_pak:
                container_record["ExtractionStatus"] = "blocked_missing_tool"
                container_record["Warnings"].append("UnrealPak.exe is not configured or does not exist.")
                report.setdefault("BlockedItems", []).append(
                    {
                        "Path": pak_label,
                        "Reason": "blocked_missing_tool",
                        "RequiredTool": "UnrealPak.exe",
                    }
                )
                report["Events"].append(
                    AnalysisEvent(
                        "error",
                        "pak_listing_tool_missing",
                        "UnrealPak.exe is not configured or does not exist; .pak contents could not be listed.",
                        path=pak_label,
                        source="unrealpak",
                        severity=95,
                    ).to_dict()
                )
                continue

            result = self.run_tool(
                report,
                "UnrealPak",
                [str(unreal_pak), str(pak_path), "-List"],
                source="unrealpak",
                container=pak_label,
            )
            if result.get("ExitCode") != 0:
                container_record["ExtractionStatus"] = "listing_failed"
                continue
            entries = parse_unrealpak_list_output(result.get("Output", ""))
            container_record["EntryCount"] = len(entries)
            container_record["ExtractionStatus"] = "listed"
            for entry in entries:
                self.add_container_entry(report, container_record, entry, "unrealpak")
                observations.append(
                    classify_path(
                        entry,
                        "unrealpak",
                        container=pak_label,
                        game_type_id="unreal5",
                    )
                )
            self.extract_unreal_pak(unreal_pak, pak_path, pak_label, container_record, extracted_root, report)
        return observations

    def collect_unreal_iostore_observations(self, content_path, report, extracted_root):
        observations = []
        retoc = self.unreal_tool_path("RetocPath")
        for utoc_path in Path(content_path).rglob("*.utoc"):
            if ".workshop_analysis" in utoc_path.parts:
                continue
            utoc_label = relative_label(content_path, utoc_path)
            container_record = self.new_container_record(utoc_label, "utoc", utoc_path)
            report.setdefault("Containers", []).append(container_record)
            ucas_path = utoc_path.with_suffix(".ucas")
            if not ucas_path.exists():
                container_record["Warnings"].append("Matching .ucas file was not found.")
                report.setdefault("BlockedItems", []).append(
                    {
                        "Path": utoc_label,
                        "Reason": "blocked_missing_pair",
                        "RequiredFile": str(ucas_path),
                    }
                )
                report["Events"].append(
                    AnalysisEvent(
                        "error",
                        "iostore_ucas_missing",
                        "Matching .ucas file was not found for this .utoc container.",
                        path=utoc_label,
                        source="retoc",
                        severity=85,
                    ).to_dict()
                )
            if not retoc:
                container_record["ExtractionStatus"] = "blocked_missing_tool"
                container_record["Warnings"].append("retoc.exe is not configured or does not exist.")
                report.setdefault("BlockedItems", []).append(
                    {
                        "Path": utoc_label,
                        "Reason": "blocked_missing_tool",
                        "RequiredTool": "retoc.exe",
                    }
                )
                report["Events"].append(
                    AnalysisEvent(
                        "error",
                        "iostore_listing_tool_missing",
                        "retoc.exe is not configured or does not exist; IO Store contents could not be listed.",
                        path=utoc_label,
                        source="retoc",
                        severity=95,
                    ).to_dict()
                )
                continue

            result = self.run_tool(
                report,
                "retoc",
                [str(retoc)] + self.retoc_global_args() + ["list", str(utoc_path)],
                source="retoc",
                container=utoc_label,
            )
            if result.get("ExitCode") != 0:
                container_record["ExtractionStatus"] = "listing_failed"
                continue
            entries = parse_retoc_list_output(result.get("Output", ""))
            container_record["EntryCount"] = len(entries)
            container_record["ExtractionStatus"] = "listed"
            for entry in entries:
                self.add_container_entry(report, container_record, entry, "retoc")
                observations.append(
                    classify_path(
                        entry,
                        "retoc",
                        container=utoc_label,
                        game_type_id="unreal5",
                    )
                )
            self.extract_unreal_iostore(retoc, utoc_path, utoc_label, container_record, extracted_root, report)
        return observations

    def extract_unreal_pak(self, unreal_pak, pak_path, pak_label, container_record, extracted_root, report):
        target_root = ensure_directory(Path(extracted_root) / safe_path_fragment(pak_label))
        result = self.run_tool(
            report,
            "UnrealPak",
            [str(unreal_pak), str(pak_path), "-Extract", str(target_root)],
            source="unrealpak",
            container=pak_label,
            timeout_seconds=600,
        )
        if result.get("ExitCode") == 0:
            container_record["ExtractionStatus"] = "extracted"
            container_record["ExtractedRoot"] = str(target_root)
            report["GeneratedFiles"].append(str(target_root))
        else:
            container_record["ExtractionStatus"] = "extraction_failed"
            container_record["Errors"].append("UnrealPak extraction failed.")

    def extract_unreal_iostore(self, retoc, utoc_path, utoc_label, container_record, extracted_root, report):
        target_root = ensure_directory(Path(extracted_root) / safe_path_fragment(utoc_label))
        command = [str(retoc)] + self.retoc_global_args() + ["unpack", str(utoc_path), str(target_root)]
        result = self.run_tool(
            report,
            "retoc",
            command,
            source="retoc",
            container=utoc_label,
            timeout_seconds=600,
        )
        if result.get("ExitCode") == 0:
            container_record["ExtractionStatus"] = "extracted"
            container_record["ExtractedRoot"] = str(target_root)
            report["GeneratedFiles"].append(str(target_root))
        else:
            container_record["ExtractionStatus"] = "extraction_failed"
            container_record["Errors"].append("retoc unpack failed.")

    def retoc_global_args(self):
        aes_key = self.unreal_aes_key()
        if aes_key:
            return ["--aes-key", aes_key]
        return []

    def unreal_aes_key(self):
        unreal5 = self.unreal_config()
        direct = unreal5.get("AesKey")
        if direct:
            return str(direct).strip()

        crypto_file = unreal5.get("CryptoKeysFile")
        if not crypto_file:
            return None
        path = Path(crypto_file)
        if not path.exists():
            return None
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = text
        key = find_aes_key(payload)
        return key

    def add_container_entry(self, report, container_record, internal_path, source):
        suffix = Path(internal_path).suffix.lower()
        report.setdefault("ContainerEntries", []).append(
            {
                "SourceContainerPath": container_record.get("SourcePath"),
                "ContainerLabel": container_record.get("Label"),
                "ContainerType": container_record.get("ContainerType"),
                "InternalPath": str(internal_path).replace("\\", "/"),
                "Extension": suffix,
                "Source": source,
                "CompressedSizeBytes": None,
                "UncompressedSizeBytes": None,
                "Encrypted": container_record.get("Encrypted"),
                "Compression": container_record.get("Compression"),
                "ExtractionStatus": container_record.get("ExtractionStatus"),
                "Warnings": [],
                "Errors": [],
            }
        )

    def unreal_tool_path(self, key):
        unreal5 = self.unreal_config()
        value = unreal5.get(key) if isinstance(unreal5, dict) else None
        if not value:
            return None
        path = Path(value)
        return path if path.exists() else None

    def unreal_config(self):
        unreal5 = self.tool_config.get("Unreal5", self.tool_config)
        return unreal5 if isinstance(unreal5, dict) else {}

    def unreal_tool_status(self):
        unreal5 = self.unreal_config()
        status = {}
        for key, display_name in (
            ("RetocPath", "retoc.exe"),
            ("UnrealPakPath", "UnrealPak.exe"),
            ("FModelPath", "FModel.exe"),
            ("CUE4ParsePath", "CUE4Parse backend"),
            ("UAssetApiPath", "UAssetAPI backend"),
            ("KismetAnalyzerPath", "kismet-analyzer"),
            ("OodlePath", "Oodle runtime"),
        ):
            value = unreal5.get(key)
            path = Path(value) if value else None
            status[key] = {
                "Name": display_name,
                "Path": str(path) if path else None,
                "Configured": bool(path and path.exists()),
            }
        status["MappingsDir"] = unreal5.get("MappingsDir")
        status["CryptoKeysFile"] = unreal5.get("CryptoKeysFile")
        status["EngineVersion"] = unreal5.get("EngineVersion") or "auto"
        status["Validation"] = unreal5.get("Validation", {})
        return status

    def collect_extracted_file_observations(self, extracted_root, report, game_type_id):
        observations = []
        root = Path(extracted_root)
        if not root.exists():
            return observations

        for path in root.rglob("*"):
            if not path.is_file():
                continue
            label = relative_label(root, path)
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            digest = file_sha256(path)
            report.setdefault("ExtractedFiles", []).append(
                {
                    "Path": str(path),
                    "RelativePath": label.replace("\\", "/"),
                    "Extension": path.suffix.lower(),
                    "SizeBytes": size,
                    "Sha256": digest,
                    "InsideContainer": True,
                }
            )
            observations.append(
                classify_path(
                    label,
                    "extracted",
                    size_bytes=size,
                    game_type_id=game_type_id,
                )
            )
        return observations

    def scan_unreal_script_files(self, content_path, extracted_root, report):
        roots = [
            ("filesystem", Path(content_path), False),
            ("extracted", Path(extracted_root), True),
        ]
        seen = set()
        for source, root, inside_container in roots:
            if not root.exists():
                continue
            for path in root.rglob("*"):
                if not path.is_file() or ".workshop_analysis" in path.parts:
                    continue
                suffix = path.suffix.lower()
                name = path.name.lower()
                if suffix not in UNREAL_SCRIPT_SCAN_EXTENSIONS and name not in INTERESTING_NAMES:
                    continue
                key = (source, str(path))
                if key in seen:
                    continue
                seen.add(key)
                try:
                    size = path.stat().st_size
                except OSError:
                    size = 0
                report.setdefault("ScriptFindings", []).append(
                    {
                        "Type": script_finding_type(path),
                        "Path": str(path),
                        "RelativePath": relative_label(root, path).replace("\\", "/"),
                        "Extension": suffix,
                        "Source": source,
                        "InsideContainer": inside_container,
                        "SizeBytes": size,
                        "Sha256": file_sha256(path),
                        "Preview": safe_text_preview(path),
                        "Severity": classify_path(path.name, source, size, game_type_id="unreal5").severity,
                    }
                )

    def parse_unreal_asset_registry(self, content_path, extracted_root, report):
        registry_files = []
        for root in (Path(content_path), Path(extracted_root)):
            if root.exists():
                registry_files.extend(path for path in root.rglob("AssetRegistry.bin") if path.is_file())

        if not registry_files:
            report.setdefault("AssetRegistry", {}).setdefault("Warnings", []).append(
                "AssetRegistry.bin was not found."
            )
            return

        asset_registry = report.setdefault("AssetRegistry", {"Files": [], "Assets": [], "Warnings": []})
        seen_assets = set()
        for path in registry_files:
            try:
                data = path.read_bytes()
            except OSError as exc:
                asset_registry.setdefault("Warnings", []).append(str(exc))
                report["Events"].append(
                    AnalysisEvent(
                        "error",
                        "asset_registry_read_error",
                        str(exc),
                        path=str(path),
                        source="asset_registry",
                        severity=80,
                    ).to_dict()
                )
                continue

            strings = extract_printable_strings(data)
            asset_paths = [
                value
                for value in strings
                if looks_like_unreal_asset_path(value) or Path(value).suffix.lower() in UNREAL_COOKED_PACKAGE_EXTENSIONS
            ]
            asset_registry["Files"].append(
                {
                    "Path": str(path),
                    "SizeBytes": len(data),
                    "Sha256": file_sha256(path),
                    "StringCount": len(strings),
                    "RecoveredAssetPathCount": len(asset_paths),
                    "Parser": "best_effort_strings",
                }
            )
            for asset_path in asset_paths:
                normalized = asset_path.replace("\\", "/")
                if normalized in seen_assets:
                    continue
                seen_assets.add(normalized)
                asset_registry["Assets"].append(
                    {
                        "Path": normalized,
                        "ClassHint": unreal_asset_class_hint(normalized),
                        "ScriptCapable": is_blueprint_like(normalized) or is_data_logic_like(normalized),
                    }
                )
        if not asset_registry["Assets"]:
            asset_registry.setdefault("Warnings", []).append(
                "AssetRegistry.bin was present but no asset paths were recovered by best-effort parsing."
            )

    def analyze_unreal_cooked_assets(self, content_path, extracted_root, report):
        candidates = self.collect_cooked_asset_candidates(content_path, extracted_root, report)
        if not candidates:
            return

        parser_path = (
            self.unreal_tool_path("CUE4ParsePath")
            or self.unreal_tool_path("UAssetApiPath")
            or self.unreal_tool_path("FModelPath")
        )
        kismet_path = self.unreal_tool_path("KismetAnalyzerPath")
        mappings = self.available_usmap_files()
        oodle_path = self.unreal_tool_path("OodlePath")

        if not mappings:
            report.setdefault("BlockedItems", []).append(
                {
                    "Path": "",
                    "Reason": "blocked_missing_mapping",
                    "RequiredTool": ".usmap mappings",
                    "AffectedItemCount": len(candidates),
                }
            )
            report["Events"].append(
                AnalysisEvent(
                    "warning",
                    "unreal_mappings_missing",
                    ".usmap mappings are not configured; UE5 unversioned property parsing may be incomplete.",
                    source="uasset",
                    severity=70,
                ).to_dict()
            )

        if not oodle_path:
            report["Events"].append(
                AnalysisEvent(
                    "warning",
                    "oodle_support_missing",
                    "Oodle runtime is not configured; Oodle-compressed chunks may remain blocked.",
                    source="uasset",
                    severity=60,
                ).to_dict()
            )

        if not parser_path:
            report.setdefault("BlockedItems", []).append(
                {
                    "Path": "",
                    "Reason": "blocked_missing_parser",
                    "RequiredTool": "CUE4Parse/FModel/UAssetAPI-compatible parser",
                    "AffectedItemCount": len(candidates),
                }
            )
            report["Events"].append(
                AnalysisEvent(
                    "warning",
                    "unreal_asset_parser_missing",
                    "No CUE4Parse, FModel, or UAssetAPI-compatible parser is configured; cooked asset internals could not be parsed.",
                    source="uasset",
                    severity=75,
                ).to_dict()
            )

        for candidate in candidates:
            finding = cooked_asset_finding(candidate)
            if finding["FindingType"] == "blueprint_asset":
                report.setdefault("BlueprintFindings", []).append(finding)
                if not kismet_path:
                    report.setdefault("BlockedItems", []).append(
                        {
                            "Path": candidate["Path"],
                            "Reason": "blocked_missing_kismet_analyzer",
                            "RequiredTool": "kismet-analyzer",
                        }
                    )
                else:
                    self.run_kismet_analyzer(kismet_path, candidate, finding, report)
            elif finding["FindingType"] != "cooked_asset":
                report.setdefault("DataLogicFindings", []).append(finding)

            if parser_path and candidate.get("FilesystemPath"):
                self.run_cooked_asset_parser(parser_path, candidate, finding, report)

    def collect_cooked_asset_candidates(self, content_path, extracted_root, report):
        candidates = []
        seen = set()
        for source, root, inside_container in (
            ("filesystem", Path(content_path), False),
            ("extracted", Path(extracted_root), True),
        ):
            if not root.exists():
                continue
            for path in root.rglob("*"):
                if not path.is_file() or path.suffix.lower() not in UNREAL_COOKED_PACKAGE_EXTENSIONS:
                    continue
                label = relative_label(root, path).replace("\\", "/")
                key = (source, label)
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(
                    {
                        "Path": label,
                        "FilesystemPath": str(path),
                        "Extension": path.suffix.lower(),
                        "Source": source,
                        "InsideContainer": inside_container,
                        "SizeBytes": safe_file_size(path),
                        "Sha256": file_sha256(path),
                    }
                )

        for entry in report.get("ContainerEntries", []):
            internal = entry.get("InternalPath") or ""
            if Path(internal).suffix.lower() not in UNREAL_COOKED_PACKAGE_EXTENSIONS:
                continue
            key = ("container", internal)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                {
                    "Path": internal,
                    "FilesystemPath": None,
                    "Extension": Path(internal).suffix.lower(),
                    "Source": entry.get("Source"),
                    "Container": entry.get("ContainerLabel"),
                    "InsideContainer": True,
                    "SizeBytes": 0,
                    "Sha256": None,
                }
            )
        return candidates

    def available_usmap_files(self):
        mappings_dir = self.unreal_config().get("MappingsDir")
        if not mappings_dir:
            return []
        path = Path(mappings_dir)
        if not path.exists():
            return []
        return [str(item) for item in path.rglob("*.usmap") if item.is_file()]

    def run_cooked_asset_parser(self, parser_path, candidate, finding, report):
        if not candidate.get("FilesystemPath"):
            return
        result = self.run_tool(
            report,
            Path(parser_path).stem,
            [str(parser_path), str(candidate["FilesystemPath"])],
            source="uasset_parser",
            container=candidate.get("Container", ""),
            timeout_seconds=120,
        )
        finding["Parser"] = {
            "Tool": Path(parser_path).name,
            "ExitCode": result.get("ExitCode"),
            "OutputPreview": safe_output_preview(result.get("Output", "")),
            "Error": result.get("Error", ""),
        }

    def run_kismet_analyzer(self, kismet_path, candidate, finding, report):
        if not candidate.get("FilesystemPath"):
            return
        result = self.run_tool(
            report,
            "kismet-analyzer",
            [str(kismet_path), str(candidate["FilesystemPath"])],
            source="kismet",
            container=candidate.get("Container", ""),
            timeout_seconds=120,
        )
        finding["Kismet"] = {
            "Tool": Path(kismet_path).name,
            "ExitCode": result.get("ExitCode"),
            "OutputPreview": safe_output_preview(result.get("Output", "")),
            "Error": result.get("Error", ""),
        }

    @staticmethod
    def new_container_record(label, container_type, source_path):
        try:
            size = Path(source_path).stat().st_size
        except OSError:
            size = 0
        return {
            "SourcePath": str(source_path),
            "ContainerType": container_type,
            "InternalPath": "",
            "Extension": Path(source_path).suffix.lower(),
            "SizeBytes": size,
            "CompressedSizeBytes": None,
            "UncompressedSizeBytes": None,
            "Encrypted": None,
            "Compression": None,
            "ExtractionStatus": "pending",
            "EntryCount": 0,
            "Warnings": [],
            "Errors": [],
            "Label": label,
        }

    def run_tool(self, report, tool_name, command, source, container="", timeout_seconds=180):
        invocation = {
            "Tool": tool_name,
            "Command": [str(part) for part in command],
            "StartedUtc": utc_now_iso(),
            "CompletedUtc": None,
            "ExitCode": None,
            "Output": "",
            "Error": "",
        }
        try:
            result = subprocess.run(
                [str(part) for part in command],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
            )
            invocation["CompletedUtc"] = utc_now_iso()
            invocation["ExitCode"] = result.returncode
            invocation["Output"] = getattr(result, "stdout", "") or ""
            if result.returncode != 0:
                report["Events"].append(
                    AnalysisEvent(
                        "error",
                        "tool_exit_nonzero",
                        "{0} exited with code {1}.".format(tool_name, result.returncode),
                        source=source,
                        container=container,
                        severity=90,
                    ).to_dict()
                )
            self.record_tool_output_blockers(report, invocation, source, container)
        except subprocess.TimeoutExpired as exc:
            invocation["CompletedUtc"] = utc_now_iso()
            invocation["Error"] = str(exc)
            report["Events"].append(
                AnalysisEvent(
                    "error",
                    "tool_timeout",
                    str(exc),
                    source=source,
                    container=container,
                    severity=90,
                ).to_dict()
            )
        except OSError as exc:
            invocation["CompletedUtc"] = utc_now_iso()
            invocation["Error"] = str(exc)
            report["Events"].append(
                AnalysisEvent(
                    "error",
                    "tool_launch_error",
                    str(exc),
                    source=source,
                    container=container,
                    severity=90,
                ).to_dict()
            )
        report["ToolInvocations"].append(invocation)
        return invocation

    def record_tool_output_blockers(self, report, invocation, source, container):
        output = "{0}\n{1}".format(invocation.get("Output") or "", invocation.get("Error") or "")
        lowered = output.lower()
        if not output.strip():
            return

        if any(token in lowered for token in ("encrypted", "aes", "crypto key", "encryption key")):
            report.setdefault("BlockedItems", []).append(
                {
                    "Path": container,
                    "Reason": "blocked_encrypted",
                    "RequiredTool": "Crypto.json or AES key",
                    "Source": source,
                }
            )
            report["Events"].append(
                AnalysisEvent(
                    "warning",
                    "unreal_encryption_detected",
                    "Tool output indicates encrypted Unreal content; provide a legitimate AES key or Crypto.json.",
                    source=source,
                    container=container,
                    severity=85,
                ).to_dict()
            )

        if "oodle" in lowered and not self.unreal_tool_path("OodlePath"):
            report.setdefault("BlockedItems", []).append(
                {
                    "Path": container,
                    "Reason": "blocked_missing_oodle",
                    "RequiredTool": "Oodle runtime",
                    "Source": source,
                }
            )
            report["Events"].append(
                AnalysisEvent(
                    "warning",
                    "unreal_oodle_missing",
                    "Tool output mentions Oodle compression, but no Oodle runtime is configured.",
                    source=source,
                    container=container,
                    severity=80,
                ).to_dict()
            )

    @staticmethod
    def write_content_marker(content_path, result):
        marker_dir = ensure_directory(Path(content_path) / ".workshop_analysis")
        marker = {
            "GeneratedUtc": result["GeneratedUtc"],
            "GameTypeId": result["GameTypeId"],
            "Mode": result.get("RequestedMode") or result.get("Mode"),
            "OutputRoot": result["OutputRoot"],
            "ObservationCount": len(result.get("Observations", [])),
            "EventCount": len(result.get("Events", [])),
        }
        (marker_dir / "analysis_complete.json").write_text(
            json.dumps(marker, indent=2) + "\n",
            encoding="utf-8",
        )


def normalize_mode(mode):
    mode = (mode or "auto").strip().lower()
    if mode in ("a", "automatic"):
        return "auto"
    if mode in ("m", "manual", "all"):
        return "manual"
    if mode in ("auto", "manual"):
        return mode
    raise ValueError("Analysis mode must be auto or manual.")


def safe_path_fragment(value):
    return "".join(character if character.isalnum() else "_" for character in str(value)).strip("_") or "archive"


def safe_file_size(path):
    try:
        return Path(path).stat().st_size
    except OSError:
        return 0


def file_sha256(path):
    path = Path(path)
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def safe_text_preview(path, limit=1200):
    path = Path(path)
    if path.suffix.lower() not in UNREAL_SCRIPT_SCAN_EXTENSIONS and path.name.lower() not in INTERESTING_NAMES:
        return ""
    try:
        raw = path.read_bytes()[: limit * 4]
    except OSError:
        return ""
    if b"\x00" in raw[:200]:
        return ""
    text = raw.decode("utf-8", errors="replace")
    return text[:limit]


def safe_output_preview(output, limit=2000):
    text = (output or "").strip().replace("\r\n", "\n")
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def script_finding_type(path):
    suffix = Path(path).suffix.lower()
    name = Path(path).name.lower()
    if suffix in SCRIPT_EXTENSIONS:
        return "loose_script"
    if name == "assetregistry.bin":
        return "asset_registry"
    return "config_file"


def extract_printable_strings(data, min_length=4):
    strings = set()
    ascii_pattern = rb"[\x20-\x7e]{%d,}" % min_length
    utf16_pattern = rb"(?:[\x20-\x7e]\x00){%d,}" % min_length
    for match in re.finditer(ascii_pattern, data):
        strings.add(match.group(0).decode("utf-8", errors="replace"))
    for match in re.finditer(utf16_pattern, data):
        strings.add(match.group(0).decode("utf-16le", errors="replace"))
    return sorted(strings)


def looks_like_unreal_asset_path(value):
    value = str(value).replace("\\", "/")
    lowered = value.lower()
    return (
        lowered.startswith(("/game/", "game/", "/engine/", "engine/", "content/"))
        or "/content/" in lowered
        or lowered.endswith(tuple(UNREAL_COOKED_PACKAGE_EXTENSIONS))
    )


def unreal_asset_class_hint(path):
    lowered = str(path).lower()
    if is_blueprint_like(lowered):
        if "anim" in lowered:
            return "animation_blueprint"
        if "widget" in lowered or "wbp_" in lowered:
            return "widget_blueprint"
        return "blueprint_asset"
    if "behavior" in lowered or "/bt_" in lowered or lowered.startswith("bt_"):
        return "behavior_tree"
    if "datatable" in lowered or "/dt_" in lowered or lowered.startswith("dt_"):
        return "data_table"
    if "dataasset" in lowered:
        return "data_asset"
    if "niagara" in lowered:
        return "niagara_logic"
    if "pcg" in lowered:
        return "data_logic"
    return "cooked_asset"


def is_blueprint_like(path):
    lowered = str(path).lower()
    return any(hint in lowered for hint in UNREAL_BLUEPRINT_HINTS)


def is_data_logic_like(path):
    lowered = str(path).lower()
    return any(hint in lowered for hint in UNREAL_DATA_LOGIC_HINTS)


def cooked_asset_finding(candidate):
    class_hint = unreal_asset_class_hint(candidate.get("Path") or "")
    severity = 85 if class_hint.endswith("blueprint") or class_hint == "blueprint_asset" else 75
    if class_hint in ("data_table", "data_asset", "behavior_tree", "niagara_logic", "data_logic", "widget_blueprint"):
        severity = 80
    return {
        "FindingType": class_hint,
        "Path": candidate.get("Path"),
        "FilesystemPath": candidate.get("FilesystemPath"),
        "Source": candidate.get("Source"),
        "Container": candidate.get("Container", ""),
        "InsideContainer": candidate.get("InsideContainer", False),
        "Extension": candidate.get("Extension"),
        "SizeBytes": candidate.get("SizeBytes", 0),
        "Sha256": candidate.get("Sha256"),
        "Severity": severity,
        "SeverityLabel": severity_label(severity),
        "Confidence": "heuristic",
        "Warnings": [],
    }


def find_aes_key(value):
    if isinstance(value, dict):
        preferred_keys = (
            "AesKey",
            "AESKey",
            "Key",
            "key",
            "EncryptionKey",
            "MainKey",
        )
        for key in preferred_keys:
            if key in value:
                found = find_aes_key(value[key])
                if found:
                    return found
        for item in value.values():
            found = find_aes_key(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = find_aes_key(item)
            if found:
                return found
    elif isinstance(value, str):
        match = re.search(r"0x[0-9a-fA-F]{64}|[0-9a-fA-F]{64}", value)
        if match:
            return match.group(0)
    return None


def parse_unrealpak_list_output(output):
    entries = []
    for line in (output or "").splitlines():
        entry = extract_unreal_path_from_line(line)
        if entry:
            entries.append(entry)
    return dedupe_preserving_order(entries)


def parse_retoc_list_output(output):
    entries = []
    for line in (output or "").splitlines():
        entry = extract_unreal_path_from_line(line)
        if entry:
            entries.append(entry)
    return dedupe_preserving_order(entries)


def extract_unreal_path_from_line(line):
    text = (line or "").strip().strip('"')
    if not text:
        return None
    if text.startswith("[") or text.lower().startswith(("warning", "error", "detected ")):
        return None

    candidates = []
    for token in text.replace("\\", "/").split():
        token = token.strip().strip('"').strip(",")
        if "/" in token or token.lower().endswith(tuple(UNREAL_ASSET_EXTENSIONS | UNREAL_PROGRAMMATIC_EXTENSIONS)):
            candidates.append(token)
    if not candidates and "/" in text:
        candidates.append(text)

    for candidate in candidates:
        lowered = candidate.lower()
        if lowered.startswith(("mount point", "log", "display:")):
            continue
        if ":" in candidate and not candidate.startswith("/"):
            continue
        return candidate.lstrip("/")
    return None


def dedupe_preserving_order(values):
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def classify_path(path, source, size_bytes=0, container="", game_type_id="source2"):
    suffix = Path(path).suffix.lower()
    name = Path(path).name.lower()
    reason = "General content"
    severity = 10

    if suffix in EXECUTABLE_EXTENSIONS:
        severity = 100
        reason = "Executable or directly runnable script"
    elif suffix in SCRIPT_EXTENSIONS:
        severity = 90
        reason = "Script or source code"
    elif game_type_id == "unreal5" and (suffix in UNREAL_PROGRAMMATIC_EXTENSIONS or name in INTERESTING_NAMES):
        severity = 80
        reason = "Unreal Engine programmatic/config data"
    elif game_type_id == "unreal5" and suffix in UNREAL_ASSET_EXTENSIONS:
        severity = 75
        reason = "Unreal Engine cooked asset or IO Store data"
    elif game_type_id == "source2" and (suffix in SOURCE2_PROGRAMMATIC_EXTENSIONS or name in INTERESTING_NAMES):
        severity = 80
        reason = "Source 2 programmatic/config data"
    elif suffix in PACKAGE_EXTENSIONS:
        severity = 70
        reason = "Package or archive container"
    elif suffix in LOW_SIGNAL_ASSET_EXTENSIONS:
        severity = 5
        reason = "Low-signal media/model asset"
    elif suffix:
        severity = 30
        reason = "Unknown extension"

    return AnalysisObservation(
        path=str(path).replace("\\", "/"),
        source=source,
        severity=severity,
        severity_label=severity_label(severity),
        reason=reason,
        size_bytes=size_bytes,
        container=container,
        include_in_auto=severity >= 70,
    )


def severity_label(severity):
    if severity >= 100:
        return "critical"
    if severity >= 90:
        return "high"
    if severity >= 70:
        return "medium"
    if severity >= 30:
        return "low"
    return "info"


def include_observation_in_auto(observation):
    if isinstance(observation, AnalysisObservation):
        return observation.include_in_auto
    return bool(observation.get("IncludeInAutomatic"))


def include_event_in_auto(event):
    return event.get("Level") in ("error", "warning") or int(event.get("Severity") or 0) >= 70


def curate_report(report, mode):
    mode = normalize_mode(mode)
    observations = report.get("Observations", [])
    events = report.get("Events", [])
    if mode == "auto":
        curated_observations = [
            observation for observation in observations if include_observation_in_auto(observation)
        ]
        curated_events = [event for event in events if include_event_in_auto(event)]
    else:
        curated_observations = list(observations)
        curated_events = list(events)

    return {
        "SchemaVersion": report.get("SchemaVersion"),
        "GameTypeId": report.get("GameTypeId"),
        "Mode": mode,
        "GeneratedUtc": report.get("GeneratedUtc"),
        "Game": report.get("Game"),
        "WorkshopItem": report.get("WorkshopItem"),
        "ContentPath": report.get("ContentPath"),
        "OutputRoot": report.get("OutputRoot"),
        "ReportPath": report.get("ReportPath"),
        "Summary": report.get("Summary", {}),
        "Findings": curated_observations,
        "Events": sorted_events(curated_events),
    }


def sort_findings(findings):
    return sorted(
        findings,
        key=lambda finding: (
            -finding.severity,
            finding.source,
            finding.container,
            finding.path.lower(),
        ),
    )


def sorted_events(events):
    return sorted(
        events,
        key=lambda event: (
            -int(event.get("Severity") or 0),
            event.get("Level") or "",
            event.get("Type") or "",
            event.get("Path") or "",
        ),
    )
