"""Config loading and validation (SPEC §4, §6). A vertical = the files sharing its name under config/.

Everything the code needs to know about a vertical lives in YAML; the code never hard-codes prompts, firms,
locations, models or prices. Validation collects every problem and refuses to start — before any spend.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PANEL_KINDS = ("human", "agent", "twist")
FIRM_KINDS = ("firm", "person", "institution", "product")
ISO_WEEK_RE = re.compile(r"^\d{4}-W\d{2}$")

_WS = re.compile(r"\s+")


class ConfigError(ValueError):
    """Raised with every validation problem joined into one message, so the operator fixes them in one pass."""


@dataclass(frozen=True)
class Location:
    key: str
    city: str
    region: str
    country: str
    timezone: str
    dataforseo_location_code: int | None


@dataclass
class ClassSpec:
    name: str
    locations: list[str]
    publish: bool = True


@dataclass
class Prompt:
    id: str
    cls: str
    text: str
    need: str | None
    country: str | None
    twin_of: str | None
    control: str | None
    panel_name: str


@dataclass
class Panel:
    path: Path
    name: str                      # path relative to config dir, posix, e.g. panels/relocation-europe.yaml
    kind: str                      # human | agent | twist
    vertical: str
    runs_per_week: int
    default_location: str | None
    classes: dict[str, ClassSpec]
    prompts: list[Prompt]
    template: str | None
    refusal_patterns: list[str]
    engines: list[str] | None      # None = all enabled engines
    engine_options: dict[str, dict[str, Any]]
    iso_week: str | None           # twist files are bound to one ISO week
    start_date: dt.date | None     # the panel is not planned before this UTC date (SPEC §13 calendar)
    sha256: str


@dataclass
class Firm:
    id: str
    canonical: str
    kind: str
    parent: str | None
    type: str
    country: str
    website: str
    aliases: list[str]
    own: bool = False


@dataclass
class EngineSpec:
    id: str
    enabled: bool
    name: str
    api: str
    model: str
    model_by_class: dict[str, str]
    supports_location: bool
    price: dict[str, Any]
    env: list[str]
    options: dict[str, Any] = field(default_factory=dict)

    def model_for_class(self, cls: str) -> str:
        return self.model_by_class.get(cls, self.model)

    def models(self) -> set[str]:
        return {self.model, *self.model_by_class.values()} - {""}


@dataclass
class EnginesConfig:
    tunables: dict[str, Any]
    engines: dict[str, EngineSpec]


@dataclass
class Vertical:
    name: str
    config_dir: Path
    panels: list[Panel]
    firms: list[Firm]
    locations: dict[str, Location]
    engines: EnginesConfig

    def prompt_by_id(self) -> dict[str, Prompt]:
        return {p.id: p for panel in self.panels for p in panel.prompts}


# ---------------------------------------------------------------- helpers

def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalize_alias(s: str) -> str:
    """Collision key for aliases: case-insensitive, whitespace collapsed. S3 adds legal-form stripping on top."""
    return _WS.sub(" ", s.strip().lower())


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        raise ConfigError(f"missing config file: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: YAML syntax error: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return data


def _substitute(template: str, need: str, country: str) -> str:
    return " ".join(template.replace("{need}", need).replace("{country}", country).split())


def _str_list(value: Any, where: str, errors: list[str]) -> list[str]:
    """A YAML field that must be a list of strings; a bare scalar would otherwise iterate per character."""
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(x, (str, int, float)) for x in value):
        errors.append(f"{where}: must be a list of strings, got {type(value).__name__}")
        return []
    return [str(x) for x in value]


# ---------------------------------------------------------------- loaders

def load_engines(config_dir: Path) -> EnginesConfig:
    raw = _load_yaml(config_dir / "engines.yaml")
    errors: list[str] = []
    engines: dict[str, EngineSpec] = {}
    raw_engines = raw.get("engines") or {}
    if not isinstance(raw_engines, dict):
        raise ConfigError("engines.yaml: 'engines' must be a mapping")
    for eid, e in raw_engines.items():
        if not isinstance(e, dict):
            errors.append(f"engines.yaml: engine {eid} must be a mapping")
            continue
        known = {"enabled", "name", "api", "model", "model_by_class", "supports_location", "price", "env"}
        mbc = e.get("model_by_class") or {}
        if not isinstance(mbc, dict):
            errors.append(f"engines.yaml: {eid}.model_by_class must be a mapping")
            mbc = {}
        price = e.get("price") or {}
        if not isinstance(price, dict):
            errors.append(f"engines.yaml: {eid}.price must be a mapping")
            price = {}
        engines[eid] = EngineSpec(
            id=str(eid), enabled=bool(e.get("enabled", False)), name=str(e.get("name", eid)), api=str(e.get("api", "")),
            model=str(e.get("model", "") or ""), model_by_class={str(k): str(v) for k, v in mbc.items()},
            supports_location=bool(e.get("supports_location", False)), price=dict(price),
            env=_str_list(e.get("env"), f"engines.yaml: {eid}.env", errors),
            options={k: v for k, v in e.items() if k not in known},
        )
    if errors:
        raise ConfigError("config invalid:\n  - " + "\n  - ".join(errors))
    tunables = {k: v for k, v in raw.items() if k != "engines"}
    return EnginesConfig(tunables=tunables, engines=engines)


def load_locations(config_dir: Path) -> dict[str, Location]:
    raw = _load_yaml(config_dir / "locations.yaml")
    out: dict[str, Location] = {}
    locs = raw.get("locations") or {}
    if not isinstance(locs, dict):
        raise ConfigError("locations.yaml: 'locations' must be a mapping")
    for key, loc in locs.items():
        if not isinstance(loc, dict):
            raise ConfigError(f"locations.yaml: location {key} must be a mapping")
        code = loc.get("dataforseo_location_code")
        out[str(key)] = Location(
            key=str(key), city=str(loc.get("city", "")), region=str(loc.get("region", "")),
            country=str(loc.get("country", "")), timezone=str(loc.get("timezone", "")),
            dataforseo_location_code=int(code) if code is not None else None,
        )
    return out


def load_panel(path: Path, config_dir: Path, errors: list[str]) -> Panel:
    raw = _load_yaml(path)
    try:
        name = path.resolve().relative_to(config_dir.resolve()).as_posix()
    except ValueError:
        name = path.name
    kind = str(raw.get("panel", "human"))
    if kind not in PANEL_KINDS:
        errors.append(f"{name}: panel kind '{kind}' not in {PANEL_KINDS}")
    classes: dict[str, ClassSpec] = {}
    raw_classes = raw.get("classes") or {}
    if not isinstance(raw_classes, dict):
        errors.append(f"{name}: 'classes' must be a mapping")
        raw_classes = {}
    for cname, spec in raw_classes.items():
        spec = spec if isinstance(spec, dict) else {}
        classes[str(cname)] = ClassSpec(name=str(cname), locations=_str_list(spec.get("locations"), f"{name}: class {cname}.locations", errors),
                                        publish=bool(spec.get("publish", True)))
    template = raw.get("template")
    prompts: list[Prompt] = []
    raw_prompts = raw.get("prompts") or []
    if not isinstance(raw_prompts, list):
        errors.append(f"{name}: 'prompts' must be a list")
        raw_prompts = []
    for i, p in enumerate(raw_prompts):
        if not isinstance(p, dict):
            errors.append(f"{name}: prompt #{i + 1} must be a mapping with id/class/text, got {type(p).__name__}")
            continue
        pid = str(p.get("id", f"#{i + 1}"))
        need = p.get("need")
        country = p.get("country")
        text = p.get("text")
        if text is None:
            if template and need is not None and country is not None:
                text = _substitute(str(template), str(need), str(country))
            else:
                errors.append(f"{name}: prompt {pid} has no text and no template/need/country to build one")
                text = ""
        prompts.append(Prompt(
            id=pid, cls=str(p.get("class", "")), text=str(text), need=str(need) if need is not None else None,
            country=str(country) if country is not None else None,
            twin_of=str(p["twin_of"]) if p.get("twin_of") is not None else None,
            control=str(p["control"]) if p.get("control") is not None else None, panel_name=name,
        ))
    eng = raw.get("engines")
    engines = _str_list(eng, f"{name}: engines", errors) if eng is not None else None
    engine_options = raw.get("engine_options") or {}
    if not isinstance(engine_options, dict) or not all(isinstance(v, dict) for v in engine_options.values()):
        errors.append(f"{name}: engine_options must be a mapping of engine id → options mapping")
        engine_options = {}
    iso_week = raw.get("iso_week")
    if iso_week is not None and not ISO_WEEK_RE.match(str(iso_week)):
        errors.append(f"{name}: iso_week '{iso_week}' must look like 2026-W41")
    start_date: dt.date | None = None
    raw_start = raw.get("start_date")
    if raw_start is not None:
        if isinstance(raw_start, dt.date):
            start_date = raw_start
        else:
            try:
                start_date = dt.date.fromisoformat(str(raw_start))
            except ValueError:
                errors.append(f"{name}: start_date '{raw_start}' must be an ISO date (YYYY-MM-DD)")
    return Panel(
        path=path, name=name, kind=kind, vertical=str(raw.get("vertical", "")),
        runs_per_week=int(raw.get("runs_per_week", 7)), default_location=raw.get("default_location"),
        classes=classes, prompts=prompts, template=str(template) if template else None,
        refusal_patterns=_str_list(raw.get("refusal_patterns"), f"{name}: refusal_patterns", errors),
        engines=engines, engine_options={str(k): dict(v) for k, v in engine_options.items()},
        iso_week=str(iso_week) if iso_week is not None else None, start_date=start_date, sha256=sha256_of(path),
    )


def load_firms(path: Path, errors: list[str]) -> list[Firm]:
    if not path.exists():
        errors.append(f"missing firm list: {path}")
        return []
    raw = _load_yaml(path)
    firms: list[Firm] = []
    raw_firms = raw.get("firms") or []
    if not isinstance(raw_firms, list):
        errors.append(f"{path.name}: 'firms' must be a list")
        raw_firms = []
    for i, f in enumerate(raw_firms):
        if not isinstance(f, dict):
            errors.append(f"{path.name}: firm #{i + 1} must be a mapping")
            continue
        fid = str(f.get("id", "") or "")
        firms.append(Firm(
            id=fid, canonical=str(f.get("canonical", "")), kind=str(f.get("kind", "firm")),
            parent=str(f["parent"]) if f.get("parent") is not None else None, type=str(f.get("type", "other")),
            country=str(f.get("country", "")), website=str(f.get("website", "") or ""),
            aliases=_str_list(f.get("aliases"), f"{path.name}: firm {fid or i + 1} aliases", errors), own=bool(f.get("own", False)),
        ))
    return firms


def load_vertical(config_dir: Path | str, vertical: str) -> Vertical:
    config_dir = Path(config_dir)
    errors: list[str] = []
    engines = load_engines(config_dir)
    locations = load_locations(config_dir)
    panel_paths = [
        config_dir / "panels" / f"{vertical}.yaml",
        config_dir / "panels" / f"{vertical}.agent.yaml",
        config_dir / "panels" / f"{vertical}.twist.yaml",
    ]
    if not panel_paths[0].exists():
        raise ConfigError(f"missing human panel: {panel_paths[0]}")
    panels = [load_panel(p, config_dir, errors) for p in panel_paths if p.exists()]
    firms = load_firms(config_dir / "firms" / f"{vertical}.yaml", errors)
    v = Vertical(name=vertical, config_dir=config_dir, panels=panels, firms=firms, locations=locations, engines=engines)
    errors.extend(validate(v))
    if errors:
        raise ConfigError("config invalid:\n  - " + "\n  - ".join(errors))
    return v


# ---------------------------------------------------------------- validation

def validate(v: Vertical) -> list[str]:
    errors: list[str] = []
    all_prompts: dict[str, Prompt] = {}
    used_locations: set[str] = set()

    for panel in v.panels:
        if panel.vertical and panel.vertical != v.name:
            errors.append(f"{panel.name}: vertical '{panel.vertical}' ≠ '{v.name}'")
        if panel.kind == "twist" and panel.iso_week is None:
            errors.append(f"{panel.name}: a twist panel must declare iso_week")
        for cname, spec in panel.classes.items():
            if not spec.locations:
                errors.append(f"{panel.name}: class {cname} has no locations")
            for loc in spec.locations:
                if loc not in v.locations:
                    errors.append(f"{panel.name}: class {cname} uses unknown location '{loc}'")
                used_locations.add(loc)
        if panel.engines is not None:
            for eid in panel.engines:
                if eid not in v.engines.engines:
                    errors.append(f"{panel.name}: engines lists unknown engine '{eid}'")
        for eid in panel.engine_options:
            if eid not in v.engines.engines:
                errors.append(f"{panel.name}: engine_options names unknown engine '{eid}'")
        for p in panel.prompts:
            if p.id in all_prompts:
                errors.append(f"prompt id {p.id} appears in both {all_prompts[p.id].panel_name} and {panel.name}")
            else:
                all_prompts[p.id] = p
            if p.cls not in panel.classes:
                errors.append(f"{panel.name}: prompt {p.id} has class '{p.cls}' not declared in classes")
            if not p.text:
                errors.append(f"{panel.name}: prompt {p.id} has empty text")

    # twin pairing (prereg §4): same need and country as the twin
    for p in all_prompts.values():
        if p.twin_of is None:
            continue
        twin = all_prompts.get(p.twin_of)
        if twin is None:
            errors.append(f"{p.panel_name}: prompt {p.id} is twin_of unknown prompt '{p.twin_of}'")
            continue
        if twin.need is not None and (twin.need, twin.country) != (p.need, p.country):
            errors.append(f"{p.panel_name}: prompt {p.id} need/country differ from its twin {twin.id} "
                          f"({p.need!r}/{p.country!r} vs {twin.need!r}/{twin.country!r})")

    # firms
    ids: dict[str, Firm] = {}
    alias_owner: dict[str, str] = {}
    for f in v.firms:
        if not f.id:
            errors.append("firm without id")
            continue
        if f.id in ids:
            errors.append(f"firm id {f.id} duplicated")
        ids[f.id] = f
        if f.kind not in FIRM_KINDS:
            errors.append(f"firm {f.id}: kind '{f.kind}' not in {FIRM_KINDS}")
        for a in [f.canonical, *f.aliases]:
            if not a:
                continue
            key = normalize_alias(a)
            if key in alias_owner and alias_owner[key] != f.id:
                errors.append(f"alias '{a}' is shared by firms {alias_owner[key]} and {f.id}")
            alias_owner.setdefault(key, f.id)
    for f in v.firms:
        if f.parent is not None and f.parent not in ids:
            errors.append(f"firm {f.id}: parent '{f.parent}' is not a known firm id")

    # engines: every model of an enabled engine must have a price row (a call without a price = money spent, answer lost)
    for eid, spec in v.engines.engines.items():
        if not spec.enabled:
            continue
        if not spec.model:
            errors.append(f"engine {eid}: model is empty")
        p_in, p_out = spec.price.get("per_1m_input"), spec.price.get("per_1m_output")
        if isinstance(p_in, dict) or isinstance(p_out, dict):
            for m in sorted(spec.models()):
                if not isinstance(p_in, dict) or m not in p_in or not isinstance(p_out, dict) or m not in p_out:
                    errors.append(f"engine {eid}: model '{m}' has no per_1m_input/per_1m_output price in engines.yaml")
        elif "per_request" not in spec.price:
            errors.append(f"engine {eid}: price must define per_1m_input/per_1m_output or per_request")
        if spec.api == "dataforseo_ai_mode" and spec.supports_location:
            for loc in sorted(used_locations):
                if loc in v.locations and v.locations[loc].dataforseo_location_code is None:
                    errors.append(f"location '{loc}' has no dataforseo_location_code but engine {eid} is enabled")
    return errors
