import asyncio
import hashlib
import os
import shutil
import subprocess
import traceback
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import discord
from lxml import etree

from persistence import sgl_repository as storage
from localization import t


def env_color(name: str, default: str = "0xD9D9D9") -> int:
    raw = os.getenv(name, default).strip()
    try:
        return int(raw, 16) if raw.lower().startswith("0x") else int(raw)
    except (TypeError, ValueError):
        return 0xD9D9D9


LOCAL_TZ = ZoneInfo(os.getenv("LOCAL_TIMEZONE", "Europe/Riga"))
PERSISTENT_ROOT = Path(os.getenv("PERSISTENT_ROOT", "/app/persistent"))
BUNDLED_TEMPLATE_PATH = Path(os.getenv("SGL_CONTRACT_BUNDLED_TEMPLATE_PATH", "/app/templates/dogovorv1.docx"))
TEMPLATE_PATH = Path(os.getenv("SGL_CONTRACT_TEMPLATE_PATH", str(PERSISTENT_ROOT / "templates" / "dogovorv1.docx")))
SYNC_BUNDLED_TEMPLATE = os.getenv("SGL_CONTRACT_SYNC_BUNDLED_TEMPLATE", "true").strip().lower() in {"1", "true", "yes", "on", "y", "да"}
OUTPUT_DIR = Path(os.getenv("SGL_CONTRACT_OUTPUT_DIR", str(PERSISTENT_ROOT / "generated" / "contracts")))
IMAGE_DPI = int(os.getenv("SGL_CONTRACT_IMAGE_DPI", "200"))
EMBED_COLOR = env_color("SGL_CONTRACT_EMBED_COLOR", "0xD9D9D9")
MAX_FILE_BYTES = int(os.getenv("SGL_CONTRACT_MAX_FILE_BYTES", str(24 * 1024 * 1024)))

PLACEHOLDERS = {
    "contractNumber": "{contractNumber}",
    "contractDate": "{contractDate}",
    "lawyerName": "{lawyerName}",
    "lawyerStatic": "{lawyerStatic}",
    "clientName": "{clientName}",
    "clientPassport": "{clientPassport}",
    "servicePrice": "{servicePrice}",
    "contractEndDate": "{contractEndDate}",
    "clientContact": "{clientContact}",
    "lawyerContact": "{lawyerContact}",
}


def format_case_number(case_number: int) -> str:
    return f"{int(case_number):03d}"


def safe_file_name(value: str) -> str:
    import re
    cleaned = re.sub(r"[^A-Za-z0-9А-Яа-я._-]+", "-", value).strip("-._")
    return cleaned[:80] or "contract"


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _ensure_template_exists() -> Path:
    TEMPLATE_PATH.parent.mkdir(parents=True, exist_ok=True)

    if BUNDLED_TEMPLATE_PATH.exists():
        should_copy = not TEMPLATE_PATH.exists()
        if TEMPLATE_PATH.exists() and SYNC_BUNDLED_TEMPLATE:
            try:
                should_copy = _file_sha256(BUNDLED_TEMPLATE_PATH) != _file_sha256(TEMPLATE_PATH)
            except Exception:
                should_copy = True

        if should_copy:
            if TEMPLATE_PATH.exists():
                backup = TEMPLATE_PATH.with_suffix(".before_sync.docx")
                try:
                    shutil.copy2(TEMPLATE_PATH, backup)
                except Exception:
                    pass
            shutil.copy2(BUNDLED_TEMPLATE_PATH, TEMPLATE_PATH)
        return TEMPLATE_PATH

    if TEMPLATE_PATH.exists():
        return TEMPLATE_PATH

    raise FileNotFoundError(t("sgbureau.contract.errors.template_missing", path=str(TEMPLATE_PATH), bundled=str(BUNDLED_TEMPLATE_PATH)))



W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML_PARSER = etree.XMLParser(remove_blank_text=False, recover=True)


def _stabilize_layout_for_libreoffice(root: etree._Element) -> None:
    """Prevent LibreOffice font reflow from clipping table text.

    Some Word templates use exact table row heights. When LibreOffice renders the
    same document with Linux font substitutions, a line can become slightly
    taller and the bottom of the text is clipped or appears shifted. Converting
    exact row heights to atLeast keeps the visual design but allows rows to grow
    by a few pixels when needed.
    """
    for tr_height in root.xpath(".//w:trHeight", namespaces={"w": W_NS}):
        if tr_height.get(f"{{{W_NS}}}hRule") == "exact":
            tr_height.set(f"{{{W_NS}}}hRule", "atLeast")


def _replace_placeholders_in_text_nodes(root: etree._Element, replacements: dict[str, str]) -> None:
    """Replace placeholders while preserving the original DOCX layout.

    The old version opened/saved the file through python-docx. That can rewrite
    parts of the package and LibreOffice may reflow the document differently.
    This function changes only text nodes inside Word paragraphs, so tables,
    spacing, drawings, headers, footers and section geometry stay exactly as in
    the template.
    """
    paragraphs = root.xpath(".//w:p", namespaces={"w": W_NS})
    for paragraph in paragraphs:
        text_nodes = paragraph.xpath(".//w:t", namespaces={"w": W_NS})
        if not text_nodes:
            continue

        for placeholder, replacement in replacements.items():
            while True:
                parts = [node.text or "" for node in text_nodes]
                full_text = "".join(parts)
                start = full_text.find(placeholder)
                if start < 0:
                    break
                end = start + len(placeholder)

                positions: list[tuple[int, int, int]] = []
                pos = 0
                for idx, part in enumerate(parts):
                    node_start = pos
                    node_end = pos + len(part)
                    positions.append((idx, node_start, node_end))
                    pos = node_end

                affected = [(idx, ns, ne) for idx, ns, ne in positions if ne > start and ns < end]
                if not affected:
                    break

                first_idx, first_start, _ = affected[0]
                last_idx, last_start, _ = affected[-1]
                first_text = text_nodes[first_idx].text or ""

                if first_idx == last_idx:
                    local_start = start - first_start
                    local_end = end - first_start
                    text_nodes[first_idx].text = first_text[:local_start] + replacement + first_text[local_end:]
                    continue

                last_text = text_nodes[last_idx].text or ""
                text_nodes[first_idx].text = first_text[: start - first_start] + replacement
                for idx, _, _ in affected[1:-1]:
                    text_nodes[idx].text = ""
                text_nodes[last_idx].text = last_text[end - last_start:]


def _replace_docx_placeholders(template_path: Path, output_docx: Path, values: dict[str, str]) -> None:
    replacements = {PLACEHOLDERS[key]: str(value or "") for key, value in values.items() if key in PLACEHOLDERS}
    output_docx.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(template_path, "r") as zin, zipfile.ZipFile(output_docx, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            should_patch = item.filename.startswith("word/") and item.filename.endswith(".xml")
            if should_patch:
                try:
                    root = etree.fromstring(data, parser=XML_PARSER)
                    _replace_placeholders_in_text_nodes(root, replacements)
                    _stabilize_layout_for_libreoffice(root)
                    data = etree.tostring(root, encoding="UTF-8", xml_declaration=True, standalone=None)
                except Exception:
                    # Fallback for rare XML parts that are not normal WordprocessingML.
                    xml_text = data.decode("utf-8", errors="ignore")
                    for placeholder, replacement in replacements.items():
                        xml_text = xml_text.replace(placeholder, replacement)
                    data = xml_text.encode("utf-8")
            zout.writestr(item, data)

def _run_command(command: list[str], cwd: Path | None = None, timeout: int = 240) -> None:
    env = os.environ.copy()
    env.setdefault("HOME", "/tmp")
    result = subprocess.run(command, cwd=str(cwd) if cwd else None, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(t("sgbureau.contract.errors.command_failed", command=" ".join(command), stdout=result.stdout[-1200:], stderr=result.stderr[-1200:]))


def _convert_docx_to_pdf(docx_path: Path, out_dir: Path) -> Path:
    soffice = shutil.which("libreoffice") or shutil.which("soffice")
    if not soffice:
        raise RuntimeError(t("sgbureau.contract.errors.libreoffice_missing"))
    profile_dir = out_dir / "lo_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    command = [
        soffice,
        "--headless",
        "--nologo",
        "--nodefault",
        "--nofirststartwizard",
        f"-env:UserInstallation=file://{profile_dir}",
        "--convert-to",
        "pdf",
        "--outdir",
        str(out_dir),
        str(docx_path),
    ]
    _run_command(command, cwd=out_dir, timeout=300)
    pdf_path = out_dir / f"{docx_path.stem}.pdf"
    if not pdf_path.exists():
        raise FileNotFoundError(t("sgbureau.contract.errors.pdf_missing", path=str(pdf_path)))
    return pdf_path


def _convert_pdf_to_jpgs(pdf_path: Path, out_dir: Path) -> list[Path]:
    pdftoppm = shutil.which("pdftoppm")
    if not pdftoppm:
        raise RuntimeError(t("sgbureau.contract.errors.pdftoppm_missing"))
    jpg_base = out_dir / "page"
    command = [pdftoppm, "-jpeg", "-r", str(IMAGE_DPI), str(pdf_path), str(jpg_base)]
    _run_command(command, cwd=out_dir, timeout=240)
    pages = sorted(out_dir.glob("page-*.jpg"), key=lambda p: [int(x) if x.isdigit() else x for x in p.stem.split("-")])
    if not pages:
        raise FileNotFoundError(t("sgbureau.contract.errors.jpg_missing", path=str(out_dir)))
    return pages


def latest_service_price(case: storage.SGLCase) -> str:
    try:
        receipts = storage.list_sgl_receipts_for_case(case.id, limit=1)
        if receipts:
            return str(receipts[0].lawyer_amount or receipts[0].total_amount or "")
    except Exception:
        pass
    return ""


def default_values(case: storage.SGLCase, guild: discord.Guild | None) -> dict[str, str]:
    now = datetime.now(LOCAL_TZ)
    end = now + timedelta(days=30)

    lawyer_member = guild.get_member(case.lead_lawyer_id) if guild else None
    client_member = guild.get_member(case.client_id) if guild else None
    lawyer_profile = None
    try:
        lawyer_profile = storage.get_lawyer_profile_for_user(case.guild_id, case.lead_lawyer_id)
    except Exception:
        lawyer_profile = None

    lawyer_name = (getattr(lawyer_profile, "lawyer_nick", None) or case.lead_lawyer_display or (lawyer_member.display_name if lawyer_member else None) or "Saoul Goodman")
    lawyer_static = (getattr(lawyer_profile, "static_id", None) or "")
    lawyer_contact = (getattr(lawyer_profile, "phone", None) or "")

    return {
        "contractNumber": f"SGL-{format_case_number(case.case_number)}",
        "contractDate": now.strftime("%d.%m.%Y"),
        "lawyerName": lawyer_name,
        "lawyerStatic": lawyer_static,
        "clientName": case.client_nick or case.client_display or (client_member.display_name if client_member else str(case.client_id)),
        "clientPassport": case.static_id or "",
        "servicePrice": latest_service_price(case),
        "contractEndDate": end.strftime("%d.%m.%Y"),
        "clientContact": case.phone or "",
        "lawyerContact": lawyer_contact,
    }


def short_placeholder(*values: str) -> str:
    text = " | ".join(v or "—" for v in values)
    return text[:100]


def parse_group(raw: str, keys: list[str], values: dict[str, str]) -> None:
    raw = (raw or "").strip()
    if not raw:
        return
    if "=" in raw:
        aliases = {
            "contractnumber": "contractNumber", "номер": "contractNumber", "number": "contractNumber",
            "contractdate": "contractDate", "дата": "contractDate",
            "contractenddate": "contractEndDate", "конец": "contractEndDate", "до": "contractEndDate",
            "lawyername": "lawyerName", "адвокат": "lawyerName", "lawyer": "lawyerName",
            "lawyerstatic": "lawyerStatic", "адвокатстатик": "lawyerStatic", "lawyerpassport": "lawyerStatic",
            "lawyercontact": "lawyerContact", "теладвоката": "lawyerContact",
            "clientname": "clientName", "клиент": "clientName", "client": "clientName",
            "clientpassport": "clientPassport", "статик": "clientPassport", "паспорт": "clientPassport",
            "clientcontact": "clientContact", "телклиента": "clientContact",
            "serviceprice": "servicePrice", "цена": "servicePrice", "стоимость": "servicePrice",
        }
        for line in re_split_lines(raw):
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            key = aliases.get(k.strip().lower().replace(" ", ""))
            if key:
                values[key] = v.strip()
        return
    parts = [p.strip() for p in re_split_values(raw) if p.strip()]
    for key, part in zip(keys, parts):
        values[key] = part


def re_split_values(raw: str) -> list[str]:
    import re
    return re.split(r"\s*[|;]\s*|\n+", raw)


def re_split_lines(raw: str) -> list[str]:
    import re
    return [p.strip() for p in re.split(r"\n+|;", raw) if p.strip()]


def generate_contract_files(case: storage.SGLCase, guild: discord.Guild | None, overrides: dict[str, str]) -> dict[str, object]:
    values = default_values(case, guild)
    values.update({k: v for k, v in overrides.items() if v is not None and str(v).strip() != ""})
    template = _ensure_template_exists()
    stamp = datetime.now(LOCAL_TZ).strftime("%Y%m%d_%H%M%S_%f")
    contract_number = safe_file_name(values["contractNumber"])
    # A manager can generate the same contract concurrently from the web and
    # Discord surfaces.  Never share an output directory or LibreOffice
    # profile between those runs: the files must remain attributable to one
    # generation attempt.
    run_dir = OUTPUT_DIR / f"{contract_number}_{stamp}_{uuid4().hex[:10]}"
    run_dir.mkdir(parents=True, exist_ok=False)
    docx_path = run_dir / f"{contract_number}.docx"
    _replace_docx_placeholders(template, docx_path, values)
    pdf_path = _convert_docx_to_pdf(docx_path, run_dir)
    pages = _convert_pdf_to_jpgs(pdf_path, run_dir)
    return {"values": values, "docx": docx_path, "pdf": pdf_path, "pages": pages, "dir": run_dir}


def file_too_large(path: Path) -> bool:
    return path.stat().st_size > MAX_FILE_BYTES


class SGLContractModal(discord.ui.Modal):
    def __init__(self, bot: discord.Client, case: storage.SGLCase, guild: discord.Guild | None) -> None:
        self.bot = bot
        self.case_number = case.case_number
        self.channel_id = case.channel_id
        defaults = default_values(case, guild)
        super().__init__(title=t("sgbureau.contract.modal_title", case_number=format_case_number(case.case_number)), timeout=900)
        self.contract_number = discord.ui.TextInput(
            label=t("sgbureau.contract.field_contract_number"),
            placeholder=short_placeholder(defaults["contractNumber"]),
            required=False,
            max_length=80,
        )
        self.dates = discord.ui.TextInput(
            label=t("sgbureau.contract.field_dates"),
            placeholder=short_placeholder(defaults["contractDate"], defaults["contractEndDate"]),
            required=False,
            max_length=120,
        )
        self.lawyer = discord.ui.TextInput(
            label=t("sgbureau.contract.field_lawyer"),
            placeholder=short_placeholder(defaults["lawyerName"], defaults["lawyerStatic"], defaults["lawyerContact"]),
            required=False,
            max_length=240,
        )
        self.client = discord.ui.TextInput(
            label=t("sgbureau.contract.field_client"),
            placeholder=short_placeholder(defaults["clientName"], defaults["clientPassport"], defaults["clientContact"]),
            required=False,
            max_length=240,
        )
        self.price = discord.ui.TextInput(
            label=t("sgbureau.contract.field_price"),
            placeholder=short_placeholder(defaults["servicePrice"] or "0"),
            required=False,
            max_length=80,
        )
        self.add_item(self.contract_number)
        self.add_item(self.dates)
        self.add_item(self.lawyer)
        self.add_item(self.client)
        self.add_item(self.price)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or interaction.channel_id is None:
            await interaction.response.send_message(t("sgbureau.contract.errors.guild_only"), ephemeral=True)
            return
        case = await asyncio.to_thread(
            storage.get_sgl_case_by_channel,
            interaction.guild.id,
            interaction.channel_id,
        )
        if case is None or case.case_number != self.case_number:
            await interaction.response.send_message(t("sgbureau.contract.errors.not_case_channel"), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        overrides: dict[str, str] = {}
        parse_group(str(self.contract_number.value), ["contractNumber"], overrides)
        parse_group(str(self.dates.value), ["contractDate", "contractEndDate"], overrides)
        parse_group(str(self.lawyer.value), ["lawyerName", "lawyerStatic", "lawyerContact"], overrides)
        parse_group(str(self.client.value), ["clientName", "clientPassport", "clientContact"], overrides)
        parse_group(str(self.price.value), ["servicePrice"], overrides)
        try:
            result = await asyncio.to_thread(generate_contract_files, case, interaction.guild, overrides)
            values: dict[str, str] = result["values"]  # type: ignore[assignment]
            pages: list[Path] = result["pages"]  # type: ignore[assignment]
            if not pages:
                raise RuntimeError("Генератор не подготовил страницы договора.")
            too_large = [p.name for p in pages if file_too_large(p)]
            if too_large:
                await interaction.followup.send(t("sgbureau.contract.errors.files_too_large", files=", ".join(too_large), dir=str(result["dir"])), ephemeral=True)
                return
            embed = discord.Embed(
                title=t("sgbureau.contract.done_title", contract_number=values["contractNumber"]),
                description=t("sgbureau.contract.done_description", client=values["clientName"], lawyer=values["lawyerName"]),
                color=EMBED_COLOR,
            )
            embed.set_footer(text=t("sgbureau.contract.footer"))
            if not isinstance(interaction.channel, discord.TextChannel):
                raise RuntimeError("Канал дела недоступен для отправки договора.")
            attachment_prefix = safe_file_name(values["contractNumber"])
            first = True
            for start in range(0, len(pages), 10):
                chunk = pages[start:start + 10]
                files = [discord.File(str(path), filename=f"{attachment_prefix}_page_{start + idx + 1}.jpg") for idx, path in enumerate(chunk)]
                await interaction.channel.send(embed=embed if first else None, files=files, allowed_mentions=discord.AllowedMentions.none())
                first = False
            await interaction.followup.send(t("sgbureau.contract.done_private", contract_number=values["contractNumber"], pages=len(pages)), ephemeral=True)
        except Exception as exc:
            traceback.print_exc()
            await interaction.followup.send(t("sgbureau.contract.errors.failed", error=str(exc)[:1800]), ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        traceback.print_exception(type(error), error, error.__traceback__)
        if interaction.response.is_done():
            await interaction.followup.send(t("sgbureau.contract.errors.failed", error=error), ephemeral=True)
        else:
            await interaction.response.send_message(t("sgbureau.contract.errors.failed", error=error), ephemeral=True)
