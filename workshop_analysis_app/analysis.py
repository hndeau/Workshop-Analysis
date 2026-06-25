"""Game-aware workshop content analysis helpers."""

import json
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
    ".ini",
    ".int",
    ".json",
    ".locmeta",
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
        if output_root.exists():
            shutil.rmtree(output_root)
        ensure_directory(expanded_root)

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
        observations.extend(self.collect_unreal_container_observations(content_path, report))
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

    def collect_unreal_container_observations(self, content_path, report):
        observations = []
        observations.extend(self.collect_unreal_pak_observations(content_path, report))
        observations.extend(self.collect_unreal_iostore_observations(content_path, report))
        return observations

    def collect_unreal_pak_observations(self, content_path, report):
        observations = []
        unreal_pak = self.unreal_tool_path("UnrealPakPath")
        for pak_path in Path(content_path).rglob("*.pak"):
            if ".workshop_analysis" in pak_path.parts:
                continue
            pak_label = relative_label(content_path, pak_path)
            if not unreal_pak:
                report["Events"].append(
                    AnalysisEvent(
                        "warning",
                        "pak_listing_tool_missing",
                        "UnrealPak.exe is not configured; package contents could not be listed.",
                        path=pak_label,
                        source="unrealpak",
                        severity=70,
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
                continue
            for entry in parse_unrealpak_list_output(result.get("Output", "")):
                observations.append(
                    classify_path(
                        entry,
                        "unrealpak",
                        container=pak_label,
                        game_type_id="unreal5",
                    )
                )
        return observations

    def collect_unreal_iostore_observations(self, content_path, report):
        observations = []
        retoc = self.unreal_tool_path("RetocPath")
        for utoc_path in Path(content_path).rglob("*.utoc"):
            if ".workshop_analysis" in utoc_path.parts:
                continue
            utoc_label = relative_label(content_path, utoc_path)
            ucas_path = utoc_path.with_suffix(".ucas")
            if not ucas_path.exists():
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
                report["Events"].append(
                    AnalysisEvent(
                        "warning",
                        "iostore_listing_tool_missing",
                        "retoc.exe is not configured; IO Store contents could not be listed.",
                        path=utoc_label,
                        source="retoc",
                        severity=70,
                    ).to_dict()
                )
                continue

            result = self.run_tool(
                report,
                "retoc",
                [str(retoc), "list", str(utoc_path)],
                source="retoc",
                container=utoc_label,
            )
            if result.get("ExitCode") != 0:
                continue
            for entry in parse_retoc_list_output(result.get("Output", "")):
                observations.append(
                    classify_path(
                        entry,
                        "retoc",
                        container=utoc_label,
                        game_type_id="unreal5",
                    )
                )
        return observations

    def unreal_tool_path(self, key):
        unreal5 = self.tool_config.get("Unreal5", self.tool_config)
        value = unreal5.get(key) if isinstance(unreal5, dict) else None
        if not value:
            return None
        path = Path(value)
        return path if path.exists() else None

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
    elif game_type_id == "unreal5" and suffix in UNREAL_PROGRAMMATIC_EXTENSIONS:
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
