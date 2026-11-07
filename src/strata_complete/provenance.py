"""W3C PROV documents of the output tiles (``prov`` command).

For every output tile, ``<tile>.prov.json`` next to its sidecar: PROV-JSON (W3C Member
Submission, 24 April 2013) for PROV-DM (W3C Recommendation, 30 April 2013). It is
rebuilt from the sidecar, so running the command again gives the same document.

Entities: the D_lite tile, the Sentinel-2 scene as served by each provider, the output
tile (a collection of its bands), the Sentinel-1 tile of the pair and the pair.
Activities: the ``complete`` run of the tile and the curator review. Agents: the tool,
the providers and the curator. Identifiers are relative to ``config.PROV_NAMESPACE``.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import TYPE_CHECKING

from strata_complete import config
from strata_complete.complete import output_sidecars, paired_s1
from strata_complete.providers import pc
from strata_complete.report import page
from strata_complete.runtime import logs
from strata_complete.runtime.progress import bar
from strata_complete.schema import ScSidecar

if TYPE_CHECKING:
    from pathlib import Path

SUFFIX = ".prov.json"
QNAME = "xsd:QName"
GEE_ASSETS = "https://earthengine.googleapis.com/v1/projects/earthengine-public/assets/"
PC_ITEMS = f"{pc.STAC_URL}/collections/{pc.COLLECTION}/items/"
PROVIDER_LABEL = {"gee": "Google Earth Engine", "pc": "Microsoft Planetary Computer"}


def _q(name: str) -> dict[str, str]:
    """A qualified-name value, as PROV-JSON writes it."""
    return {"$": name, "type": QNAME}


class _Doc:
    """Minimal PROV-JSON writer: elements by id, relations with generated blank ids."""

    def __init__(self, prefixes: dict[str, str]) -> None:
        self.data: dict[str, dict] = {"prefix": prefixes}
        self._n = 0

    def element(self, kind: str, ident: str, **attrs: object) -> str:
        self.data.setdefault(kind, {})[ident] = {k: v for k, v in attrs.items() if v is not None}
        return ident

    def relation(self, kind: str, **props: object) -> None:
        self._n += 1
        self.data.setdefault(kind, {})[f"_:{kind}{self._n}"] = {
            k: v for k, v in props.items() if v is not None
        }


def _p(local: str) -> str:
    """PROV attribute names use a prefix: ``sc:`` for this tool's own attributes."""
    return f"sc:{local}"


def _add_pair(doc: _Doc, run: str, out: str, pair_id: str, s1: dict) -> None:
    """The pair's Sentinel-1 tile, copied by the run, and the pair as a collection."""
    s1_src = doc.element(
        "entity",
        f"dlite:{s1['filename']}",
        **{
            "prov:label": "D_lite S1 tile (published, input)",
            _p("xxhash64"): s1["integrity"]["checksum"],
        },
    )
    s1_out = doc.element(
        "entity",
        f"sc:{s1['filename'].removesuffix('.tif')}",
        **{
            "prov:label": "Sentinel-1 tile (copied unchanged)",
            _p("xxhash64"): s1["integrity"]["checksum"],
            _p("bands"): " ".join(s1["dimensions"]["bands"]),
        },
    )
    pair = doc.element(
        "entity",
        f"sc:pair/{pair_id}",
        **{"prov:type": _q("prov:Collection"), "prov:label": "S1-S2 pair"},
    )
    doc.relation("used", **{"prov:activity": run, "prov:entity": s1_src})
    doc.relation("wasGeneratedBy", **{"prov:entity": s1_out, "prov:activity": run})
    doc.relation(
        "wasDerivedFrom",
        **{"prov:generatedEntity": s1_out, "prov:usedEntity": s1_src, "prov:activity": run},
    )
    doc.relation("hadMember", **{"prov:collection": pair, "prov:entity": out})
    doc.relation("hadMember", **{"prov:collection": pair, "prov:entity": s1_out})


def tile_document(sc: ScSidecar, review: dict | None, s1: dict | None = None) -> dict:
    """The PROV-JSON document of one output tile, from its sidecar and optional marks.

    ``s1`` is the D_lite sidecar of the pair's Sentinel-1 tile as copied to the
    output (plain JSON), if it is there.
    """
    tile = sc.source.dlite_filename.removesuffix(".tif")
    doc = _Doc(
        {
            "sc": config.PROV_NAMESPACE,
            "dlite": f"https://doi.org/{sc.source.dlite_doi}#",
            "gee": GEE_ASSETS,
            "pc": PC_ITEMS,
        }
    )
    tool = doc.element(
        "agent",
        f"sc:agent/strata-complete-{sc.tool.version}",
        **{
            "prov:type": _q("prov:SoftwareAgent"),
            "prov:label": "strata-complete",
            _p("version"): sc.tool.version,
            _p("baseImage"): sc.tool.base_image,
            _p("providerVersions"): json.dumps(sc.tool.provider_versions, sort_keys=True),
        },
    )
    providers = {
        key: doc.element(
            "agent",
            f"sc:agent/{key}",
            **{"prov:type": _q("prov:SoftwareAgent"), "prov:label": PROVIDER_LABEL[key]},
        )
        for key in sorted({b.provider for b in sc.bands if b.provider})
    }

    source = doc.element(
        "entity",
        f"dlite:{sc.source.dlite_filename}",
        **{
            "prov:label": "D_lite tile (published, input)",
            _p("pairId"): sc.source.pair_id,
            _p("tileId"): sc.source.tile_id,
            _p("xxhash64"): sc.source.dlite_checksum,
        },
    )
    rad = sc.radiometry
    scenes = {
        "gee": doc.element(
            "entity",
            f"gee:{sc.source.image_id}",
            **{
                "prov:label": "Sentinel-2 L2A scene as served by GEE",
                _p("esaProductId"): rad.product_id,
                _p("processingBaseline"): rad.processing_baseline,
            },
        )
    }
    # The product name Planetary Computer was asked for: the D_lite one, else the one GEE
    # reported, else the one a fallback recorded.
    pc_product = (
        sc.source.product_id or rad.product_id or (sc.fallback.product_id if sc.fallback else None)
    )
    if "pc" in providers and pc_product:
        scenes["pc"] = doc.element(
            "entity",
            f"pc:{pc.item_id(pc_product)}",
            **{
                "prov:label": "Sentinel-2 L2A scene as served by Planetary Computer",
                _p("esaProductId"): pc_product,
            },
        )
    fb = sc.fallback

    end = sc.created
    start = end - timedelta(seconds=sc.timings_s.get("total", 0.0))
    run = doc.element(
        "activity",
        f"sc:run/complete/{tile}",
        **{
            "prov:startTime": start.isoformat(),
            "prov:endTime": end.isoformat(),
            "prov:label": "strata-complete complete",
            _p("fallbackTried"): fb is not None,
            _p("fallbackUsed"): bool(fb and fb.used),
        },
    )
    doc.relation("wasAssociatedWith", **{"prov:activity": run, "prov:agent": tool})
    doc.relation("used", **{"prov:activity": run, "prov:entity": source})
    for scene in scenes.values():
        doc.relation("used", **{"prov:activity": run, "prov:entity": scene})

    out = doc.element(
        "entity",
        f"sc:{tile}",
        **{
            "prov:type": _q("prov:Collection"),
            "prov:label": "STRATA-S12-SC tile",
            _p("verdict"): sc.verdict,
            _p("xxhash64"): sc.integrity.checksum,
            _p("crs"): sc.grid.crs,
            _p("referenceIdentical"): bool(sc.reference_check)
            and all(r.identical for r in sc.reference_check),
            _p("pairedSentinel1"): sc.paired_sar.image_id,
            _p("bandsNotAdded"): " ".join(n.name for n in sc.not_added) or None,
        },
    )
    doc.relation(
        "wasGeneratedBy",
        **{"prov:entity": out, "prov:activity": run, "prov:time": end.isoformat()},
    )
    doc.relation(
        "wasDerivedFrom",
        **{"prov:generatedEntity": out, "prov:usedEntity": source, "prov:activity": run},
    )

    for b in sc.bands:
        band = doc.element(
            "entity",
            f"sc:{tile}/{b.name}",
            **{
                "prov:label": f"band {b.name}",
                _p("index"): b.index,
                _p("origin"): b.origin,
                _p("status"): b.status,
                _p("nativeResolutionM"): b.native_resolution_m,
                _p("xxhash64"): b.xxhash64,
            },
        )
        doc.relation("hadMember", **{"prov:collection": out, "prov:entity": band})
        if b.status != "ok":
            continue
        if b.origin == "dlite":
            used = source
        else:
            used = scenes[b.provider or "gee"]
            doc.relation(
                "wasAttributedTo",
                **{"prov:entity": band, "prov:agent": providers[b.provider or "gee"]},
            )
        doc.relation(
            "wasDerivedFrom",
            **{"prov:generatedEntity": band, "prov:usedEntity": used, "prov:activity": run},
        )

    if s1:
        _add_pair(doc, run, out, sc.source.pair_id, s1)

    if review:
        curator = doc.element(
            "agent", "sc:agent/curator", **{"prov:type": _q("prov:Person"), "prov:label": "curator"}
        )
        act = doc.element(
            "activity",
            f"sc:review/{tile}",
            **{"prov:endTime": review.get("at"), "prov:label": "curator review"},
        )
        mark = doc.element(
            "entity",
            f"sc:review/{tile}/mark",
            **{
                "prov:label": "curator mark (review.json)",
                _p("flag"): bool(review.get("flag")),
                _p("note"): review.get("note") or None,
            },
        )
        doc.relation("wasAssociatedWith", **{"prov:activity": act, "prov:agent": curator})
        doc.relation("used", **{"prov:activity": act, "prov:entity": out})
        doc.relation("wasGeneratedBy", **{"prov:entity": mark, "prov:activity": act})
    return doc.data


def write_folder(folder: Path) -> tuple[int, Path]:
    """Write ``<tile>.prov.json`` for every output tile of ``folder``; returns (count, log)."""
    marks = page.load_review(folder)
    s1_index = paired_s1(folder)
    written = []
    for path in bar(output_sidecars(folder), desc="prov", unit="tile"):
        sc = ScSidecar.model_validate_json(path.read_text(encoding="utf-8"))
        name = path.with_suffix(".tif").name
        s1_path = s1_index.get(sc.source.pair_id)
        s1 = json.loads(s1_path.read_text(encoding="utf-8")) if s1_path else None
        doc = tile_document(sc, marks.get(name), s1)
        target = path.with_name(path.stem + SUFFIX)
        target.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        written.append(str(target.relative_to(folder)))
    report = logs.write(
        "prov",
        {
            "folder": str(folder),
            "format": "PROV-JSON",
            "namespace": config.PROV_NAMESPACE,
            "files": written,
        },
    )
    return len(written), report
