import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web" / "consensus"
ATLAS_WEB = ROOT / "web" / "atlas"


class WebAssetContractTests(unittest.TestCase):
    PAGE_SCRIPTS = {
        "admin.html": ("admin.js", "reactor.js"),
        "index.html": ("app.js",),
        "portal.html": ("portal.js",),
        "ovr.html": ("ovr.js",),
        "games.html": ("games.js",),
        "login.html": ("login.js",),
        "egg.html": ("egg.js",),
    }

    def test_javascript_dom_references_exist_and_html_ids_are_unique(self) -> None:
        problems: list[str] = []
        for html_name, scripts in self.PAGE_SCRIPTS.items():
            html = (WEB / html_name).read_text(encoding="utf-8")
            id_values = re.findall(r'\bid=["\']([^"\']+)', html)
            ids = set(id_values)
            duplicates = sorted(
                element_id
                for element_id in ids
                if id_values.count(element_id) > 1
            )
            if duplicates:
                problems.append(f"{html_name}: duplicate ids {duplicates}")

            references: set[str] = set()
            for script in scripts:
                source = (WEB / script).read_text(encoding="utf-8")
                references.update(
                    re.findall(r'byId\(["\']([^"\']+)', source)
                )
                references.update(
                    re.findall(r'getElementById\(["\']([^"\']+)', source)
                )
            missing = sorted(references - ids)
            if missing:
                problems.append(f"{html_name}: missing ids {missing}")
        self.assertEqual(problems, [])

    def test_every_local_asset_reference_exists(self) -> None:
        missing: list[str] = []
        sources = [*WEB.glob("*.html"), *WEB.glob("*.css")]
        for path in sources:
            text = path.read_text(encoding="utf-8")
            for asset_name in re.findall(r'["\']?/assets/([^"\')?]+)', text):
                if not (WEB / asset_name).is_file():
                    missing.append(f"{path.name}: {asset_name}")
        self.assertEqual(sorted(set(missing)), [])

    def test_production_surfaces_have_accessible_font_floor(self) -> None:
        offenders: list[str] = []
        for name in (
            "admin.css",
            "style.css",
            "portal-theme.css",
            "login.css",
            "games.css",
            "ovr.css",
        ):
            source = (WEB / name).read_text(encoding="utf-8")
            for size in re.findall(r"font-size:\s*([0-9.]+)px", source):
                if float(size) < 10:
                    offenders.append(f"{name}: {size}px")
        self.assertEqual(offenders, [])

    def test_atlas_dom_contract_and_assets(self) -> None:
        html = (ATLAS_WEB / "index.html").read_text(encoding="utf-8")
        source = (ATLAS_WEB / "app.js").read_text(encoding="utf-8")
        id_values = re.findall(r'\bid=["\']([^"\']+)', html)
        ids = set(id_values)
        references = set(re.findall(r'byId\(["\']([^"\']+)', source))

        self.assertEqual(sorted(item for item in ids if id_values.count(item) > 1), [])
        self.assertEqual(sorted(references - ids), [])
        self.assertTrue((ATLAS_WEB / "style.css").is_file())
        self.assertTrue((ATLAS_WEB / "favicon.svg").is_file())


if __name__ == "__main__":
    unittest.main()
