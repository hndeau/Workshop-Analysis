"""Game-aware workshop content analysis helpers."""

import json
import shutil
import struct
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

PACKAGE_EXTENSIONS = {".vpk", ".zip"}

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
    def __init__(self, state_root):
        self.state_root = Path(state_root)

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
            return self.analyze_unreal5_stub(game, workshop_item, content_path, mode)
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
        observations.extend(self.collect_filesystem_observations(content_path, report))
        observations.extend(self.expand_zip_archives(content_path, expanded_root, report))
        observations.extend(self.collect_vpk_observations(content_path, report))
        report["Observations"] = [observation.to_dict() for observation in sort_findings(observations)]

        self.finalize_report(report)
        self.write_report(output_root, report)
        self.write_content_marker(content_path, report)
        return curate_report(report, mode)

    def analyze_unreal5_stub(self, game, workshop_item, content_path, mode="auto"):
        content_path = Path(content_path)
        output_root = self.analysis_root(game, workshop_item)
        if output_root.exists():
            shutil.rmtree(output_root)
        ensure_directory(output_root)

        report = self.new_report(game, workshop_item, content_path, output_root, "unreal5", mode)
        report["Events"].append(
            AnalysisEvent(
                "warning",
                "analysis_stub",
                "Unreal Engine 5 analysis is not implemented yet.",
                source="analysis",
                severity=70,
            ).to_dict()
        )
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
            "Observations": [],
            "Events": [],
            "GeneratedFiles": [],
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

    def collect_filesystem_observations(self, content_path, report):
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
            observations.append(classify_path(label, "filesystem", size_bytes=size))
        return observations

    def expand_zip_archives(self, content_path, expanded_root, report):
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


def classify_path(path, source, size_bytes=0, container=""):
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
    elif suffix in SOURCE2_PROGRAMMATIC_EXTENSIONS or name in INTERESTING_NAMES:
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
