#!/usr/bin/env python3
"""Build the public Atlas legal documents served by atlas.tvr.lat."""

from __future__ import annotations

from pathlib import Path
from shutil import copyfile

from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont as ReportLabTTFont
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "output" / "pdf"
PUBLIC_DIR = ROOT / "web" / "atlas-billing" / "documents"
TMP_DIR = ROOT / "tmp" / "pdfs" / "atlas-legal"
SELLER = "ИП Саниев Муртазали Бухариевич"
EMAIL = "tvr@ultra---industries.com"
EDITION = "15 сентября 2026 года"


OFFER_SECTIONS = [
    ("1. Исполнитель и статус документа", [
        f"Исполнитель - {SELLER}, ИНН 370266611106, ОГРНИП 322370000004857. Настоящий документ является публичным предложением заключить договор оказания цифровой услуги Atlas на изложенных ниже условиях.",
        "Оферта адресована дееспособным физическим лицам. Актуальная версия постоянно доступна по адресу https://atlas.tvr.lat/offer. Местом заключения договора является место нахождения Исполнителя.",
    ]),
    ("2. Термины", [
        "Atlas - программно-информационный сервис для поиска, анализа, подготовки документов и иных пользовательских задач. T-Mod Account - учётная запись, через которую Пользователь получает услугу и видит историю заказов.",
        "Atlas Token (AT) - внутренняя расчётная единица оплаченного вычислительного резерва. AT не являются валютой, электронными денежными средствами, ценной бумагой или самостоятельным средством платежа и не могут передаваться вне сервиса.",
    ]),
    ("3. Предмет договора", [
        "Исполнитель предоставляет Пользователю выбранный объём ИИ-вычислений: месячный пакет в составе тарифа либо отдельный пакет AT. Наименование, объём, срок и итоговая цена показываются в карточке продукта и окне подтверждения непосредственно перед оплатой.",
        "Ручные функции Atlas, для которых не привлекается платный ИИ-провайдер, предоставляются бесплатно. Состав доступных функций может развиваться без уменьшения уже оплаченного вычислительного резерва.",
    ]),
    ("4. Заключение договора", [
        "До оплаты Пользователь входит в собственный T-Mod Account, выбирает продукт, проверяет цену и состав, подтверждает согласие с офертой и политикой обработки персональных данных, после чего переходит на страницу Robokassa.",
        "Акцептом оферты и моментом заключения договора является успешная оплата. Пользователь обязан указывать достоверные сведения и сохранять в тайне данные доступа к аккаунту.",
    ]),
    ("5. Цена и порядок оплаты", [
        "Все цены указаны в российских рублях и включают применимые налоги Исполнителя. Актуальные цены опубликованы по адресу https://atlas.tvr.lat/#plans. Оплата производится единовременно через Robokassa доступным на её стороне способом.",
        "Автоматическое продление и рекуррентные списания не используются. Реквизиты банковской карты вводятся на стороне платёжного оператора и не поступают в T-Mod.",
    ]),
    ("6. Оказание и получение услуги", [
        "Услуга является цифровой, поэтому физическая доставка и её стоимость отсутствуют. После защищённого серверного подтверждения Robokassa выбранный пакет автоматически зачисляется в T-Mod Account, обычно немедленно и не позднее 15 минут.",
        "Момент зачисления фиксируется в истории операций. Если пакет не появился в указанный срок, Пользователь направляет обращение Исполнителю с номером заказа.",
    ]),
    ("7. Правила Atlas Token и подписки", [
        "1 AT соответствует внутреннему резерву для покрытия 0,00001 доллара США фактической стоимости вычисления у подключённого ИИ-провайдера. Списание выполняется после запроса по фактическому отчёту провайдера. Локальная операция с нулевой стоимостью не расходует AT.",
        "Месячный пакет платного тарифа действует 30 календарных дней с момента зачисления и автоматически не продлевается. Повторная покупка того же тарифа добавляет 30 дней к действующему сроку. При переходе на другой тариф новый период начинается после оплаты, а прежний остаток сохраняется до своей исходной даты окончания.",
        "Неиспользованный остаток месячного пакета прекращает действие по окончании указанного для него срока. AT, купленные отдельным пакетом, не имеют календарного срока сгорания, пока существует аккаунт и действует договор.",
    ]),
    ("8. Права и обязанности сторон", [
        "Исполнитель обязан зачислить оплаченный резерв, вести историю операций, принимать обращения и применять разумные меры защиты. Пользователь вправе видеть цену до оплаты, историю заказов и расхода, получать поддержку и заявлять требования в соответствии с законом.",
        "Пользователь обязан не обходить ограничения доступа, не создавать вредоносную нагрузку, не использовать сервис для нарушения закона и проверять юридически либо фактически значимые решения по первичным источникам.",
    ]),
    ("9. Отказ от услуги и возврат", [
        f"Запрос направляется на {EMAIL} с номером заказа, логином T-Mod, суммой, причиной и требованием. До зачисления услуги Пользователь может потребовать отмену заказа и полный возврат.",
        "После зачисления запрос рассматривается с учётом фактически использованной части: подтверждённый неиспользованный оплаченный остаток может быть возвращён, а выполненные и отражённые в истории ИИ-вычисления считаются оказанной услугой. При ненадлежащем оказании сохраняются все обязательные права Пользователя.",
    ]),
    ("10. Особенности результата Atlas", [
        "Исполнитель предоставляет доступ к программному сервису, но не гарантирует заранее определённый результат интеллектуальной обработки. ИИ может ошибаться, а внешние источники и провайдеры могут быть временно недоступны.",
        "Ответ Atlas не является официальным решением суда, администрации игрового проекта, государственного органа или профессиональной юридической консультацией.",
    ]),
    ("11. Ответственность", [
        "Стороны отвечают в пределах применимого законодательства. При подтверждённом техническом сбое Исполнитель восстанавливает баланс либо срок доступа соразмерно недоступности. Пользователь отвечает за сохранность доступа и последствия передачи PIN или сессии третьим лицам.",
        "Никакое положение оферты не ограничивает права потребителя, которые не могут быть ограничены договором.",
    ]),
    ("12. Персональные данные", [
        "Обработка выполняется по политике, опубликованной по адресу https://atlas.tvr.lat/privacy. Для заказа используются идентификатор аккаунта, номер заказа, сумма и статус оплаты. Платёжные реквизиты обрабатывает Robokassa.",
        "Пользователь может запросить доступ, исправление, удаление, ограничение или отзыв согласия через https://atlas.tvr.lat/data-request.",
    ]),
    ("13. Изменение и прекращение условий", [
        "Новая редакция публикуется с датой обновления и применяется к новым заказам после публикации. К оплаченному заказу применяется редакция, действовавшая при оплате, если последующее изменение не улучшает положение Пользователя или не требуется законом.",
    ]),
    ("14. Обращения, споры и реквизиты", [
        f"До обращения за иными способами защиты рекомендуется направить претензию на {EMAIL}, указав имя, логин T-Mod, номер заказа, требование и обстоятельства. Обращения рассматриваются в сроки, установленные применимым законодательством.",
        f"Исполнитель: {SELLER}. ИНН 370266611106. ОГРНИП 322370000004857. Сайт услуги: https://atlas.tvr.lat/.",
    ]),
]


PRIVACY_SECTIONS = [
    ("1. Оператор персональных данных", [
        f"Оператор - {SELLER}, ИНН 370266611106, ОГРНИП 322370000004857, проект T-Mod / Atlas. Адрес для обращений о данных: {EMAIL}.",
    ]),
    ("2. Область действия", [
        "Политика применяется при просмотре atlas.tvr.lat, создании и использовании T-Mod Account, покупке подписки или пакета AT, работе с Atlas и обращении в поддержку. Внешние сайты действуют по собственным правилам.",
    ]),
    ("3. Состав обрабатываемых данных", [
        "Учётная запись: Discord ID, отображаемое имя, логин, хэш PIN, роли, права и сведения о сессии.",
        "Использование Atlas: запросы, ответы, история диалогов, выбранные пространства, обратная связь, загруженные документы, изображения и распознанный текст.",
        "Расчёты: тариф, баланс, объём вычислений, использованная модель, фактическая стоимость, номер, сумма, время и статус заказа, идентификатор операции Robokassa.",
        "Технические данные: время и маршрут запроса, тип браузера и устройства, диагностические события, обезличенный хэш сетевого адреса и защитные журналы. Обращения: email, указанные Пользователем идентификаторы, содержание запроса и история ответа.",
        "Atlas не получает и не хранит номер банковской карты, CVC и платёжный пароль. Они вводятся на стороне Robokassa.",
    ]),
    ("4. Цели обработки", [
        "Создание и защита аккаунта; единая авторизация; предоставление Atlas; ведение истории и контекста; расчёт и списание AT; создание и подтверждение заказов; поддержка; предотвращение злоупотреблений; диагностика, резервное копирование и исполнение запросов Пользователя.",
    ]),
    ("5. Правовые основания", [
        "В зависимости от операции основанием является согласие Пользователя, заключение и исполнение договора, выполнение установленной законом обязанности либо законный интерес в безопасности и устойчивости сервиса.",
        "Отзыв согласия не отменяет правомерность обработки, выполненной до отзыва, и не затрагивает данные, которые требуется хранить по закону или для расчётов по договору.",
    ]),
    ("6. Платёжные данные", [
        "Robokassa получает идентификатор магазина, сумму, номер и описание заказа, технический идентификатор Пользователя и фискальные сведения о позиции. После оплаты Atlas получает подписанное уведомление о сумме, номере заказа и статусе операции.",
        "Эти данные используются для однократного зачисления услуги, бухгалтерского учёта, возврата и разрешения споров.",
    ]),
    ("7. Получатели и передача", [
        "В необходимом объёме данные могут передаваться: Robokassa - для платежа и возврата; поставщикам серверной инфраструктуры - для размещения и защиты; выбранным ИИ-провайдерам - для обработки запроса; Discord - для связанных функций аккаунта; уполномоченным администраторам - для поддержки и безопасности; государственным органам - когда это обязательно по закону.",
        "Персональные данные не продаются рекламным сетям.",
    ]),
    ("8. Сроки и место хранения", [
        "Основные данные хранятся в защищённом контуре T-Mod столько, сколько существует аккаунт и требуется для услуги. Платёжные и договорные сведения сохраняются в обязательные сроки учёта. Журналы безопасности могут храниться дольше для расследования инцидентов и защиты прав.",
        "После обоснованного удаления остаточные копии прекращают существование по циклу ротации резервных копий, если закон не требует хранения.",
    ]),
    ("9. Безопасность", [
        "Используются HTTPS, разграничение доступа, серверная проверка сессии и подписи платежа, хэширование PIN, журналирование, резервные копии и ограничение внутренних интерфейсов. Пользователь должен хранить доступ в тайне и сообщать о подозрительном входе.",
    ]),
    ("10. Права Пользователя", [
        "Пользователь вправе запросить сведения об обработке, копию данных, исправление, удаление, ограничение, перенос; возразить против обработки или отозвать согласие - в пределах применимого законодательства.",
        f"Запрос подаётся через https://atlas.tvr.lat/data-request либо на {EMAIL}. Для защиты данных Оператор проверяет личность заявителя.",
    ]),
    ("11. Cookies и локальное хранилище", [
        "Технические cookies обеспечивают единую авторизацию T-Mod, защиту сессии и проведение заказа. В браузере могут сохраняться настройки интерфейса. Рекламные cookies на atlas.tvr.lat не используются. Обязательные cookies нельзя отключить без потери возможности войти и оплатить заказ.",
    ]),
    ("12. Автоматизированная обработка", [
        "Atlas формирует ответы и рекомендации с применением ИИ, однако не принимает окончательных юридических или административных решений. Пользователь самостоятельно проверяет значимые выводы и вправе обратиться к человеку через поддержку.",
    ]),
    ("13. Изменение политики и контакты", [
        f"Актуальная редакция доступна по адресу https://atlas.tvr.lat/privacy. При существенных изменениях обновляются дата и версия; при необходимости запрашивается новое согласие. Вопросы направляются на {EMAIL}.",
    ]),
]


def _instance_font(source: Path, destination: Path, weight: int) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    font = TTFont(str(source))
    instance = instantiateVariableFont(font, {"wght": weight}, inplace=False, optimize=True)
    instance.save(str(destination))
    return destination


def _register_fonts() -> None:
    source = Path.home() / "Library" / "Fonts" / "Montserrat-VariableFont_wght.ttf"
    if source.exists():
        regular = _instance_font(source, TMP_DIR / "AtlasBody.ttf", 400)
        heading = _instance_font(source, TMP_DIR / "AtlasHeading.ttf", 650)
    else:
        # Production generation falls back to a system font with complete
        # Cyrillic and Latin coverage rather than emitting missing glyphs.
        regular = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
        heading = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
        if not regular.exists():
            regular = Path("/System/Library/Fonts/Supplemental/Verdana.ttf")
            heading = Path("/System/Library/Fonts/Supplemental/Verdana Bold.ttf")
    pdfmetrics.registerFont(ReportLabTTFont("AtlasBody", str(regular)))
    pdfmetrics.registerFont(ReportLabTTFont("AtlasHeading", str(heading)))


class AtlasDocument(BaseDocTemplate):
    def __init__(self, filename: Path, *, title: str, subject: str):
        super().__init__(
            str(filename), pagesize=A4, leftMargin=23 * mm, rightMargin=23 * mm,
            topMargin=28 * mm, bottomMargin=24 * mm, title=title, author=SELLER,
            subject=subject, creator="T-Mod Atlas",
        )
        frame = Frame(self.leftMargin, self.bottomMargin, self.width, self.height, id="main")
        self.addPageTemplates(PageTemplate(id="atlas", frames=[frame], onPage=self._decorate))
        self.document_title = title

    def _decorate(self, canvas, doc):
        canvas.saveState()
        width, height = A4
        canvas.setStrokeColor(colors.HexColor("#DCE6EF"))
        canvas.setLineWidth(.35)
        canvas.line(23 * mm, height - 18 * mm, width - 23 * mm, height - 18 * mm)
        canvas.setFont("AtlasHeading", 6.7)
        canvas.setFillColor(colors.HexColor("#133554"))
        canvas.drawString(23 * mm, height - 14 * mm, "ATLAS  /  TVR x SGL")
        canvas.setFont("AtlasBody", 7.5)
        canvas.setFillColor(colors.HexColor("#62778A"))
        canvas.drawRightString(width - 23 * mm, height - 14 * mm, "ПРАВОВОЙ ДОКУМЕНТ")
        canvas.line(23 * mm, 17 * mm, width - 23 * mm, 17 * mm)
        canvas.drawString(23 * mm, 11 * mm, "atlas.tvr.lat")
        canvas.drawCentredString(width / 2, 11 * mm, self.document_title)
        canvas.drawRightString(width - 23 * mm, 11 * mm, f"{doc.page}")
        canvas.restoreState()


def _styles():
    styles = getSampleStyleSheet()
    return {
        "label": ParagraphStyle("label", parent=styles["Normal"], fontName="AtlasHeading", fontSize=7.5, leading=10, textColor=colors.HexColor("#2573AD"), spaceAfter=8),
        "title": ParagraphStyle("title", parent=styles["Title"], fontName="AtlasHeading", fontSize=25, leading=32, alignment=TA_LEFT, textColor=colors.HexColor("#0C1A26"), spaceAfter=12),
        "subtitle": ParagraphStyle("subtitle", parent=styles["Normal"], fontName="AtlasBody", fontSize=10.5, leading=17, textColor=colors.HexColor("#52697C"), spaceAfter=18),
        "section": ParagraphStyle("section", parent=styles["Heading2"], fontName="AtlasHeading", fontSize=10, leading=15, textColor=colors.HexColor("#0B2A43"), spaceBefore=8, spaceAfter=6, keepWithNext=True),
        "body": ParagraphStyle("body", parent=styles["BodyText"], fontName="AtlasBody", fontSize=9.2, leading=14.4, textColor=colors.HexColor("#263B4B"), spaceAfter=6, alignment=TA_LEFT),
        "meta": ParagraphStyle("meta", parent=styles["Normal"], fontName="AtlasBody", fontSize=8, leading=12, textColor=colors.HexColor("#456176")),
        "closing": ParagraphStyle("closing", parent=styles["Normal"], fontName="AtlasBody", fontSize=8.5, leading=14, alignment=TA_CENTER, textColor=colors.HexColor("#41596C")),
    }


def _build(path: Path, *, title: str, label: str, subtitle: str, sections: list[tuple[str, list[str]]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    style = _styles()
    story = [Spacer(1, 9 * mm), Paragraph(label, style["label"]), Paragraph(title, style["title"]), Paragraph(subtitle, style["subtitle"])]
    metadata = Table([
        [Paragraph("РЕДАКЦИЯ", style["meta"]), Paragraph("ИСПОЛНИТЕЛЬ", style["meta"])],
        [Paragraph(EDITION, style["meta"]), Paragraph(SELLER, style["meta"])],
        [Paragraph("ПОСТОЯННЫЙ АДРЕС", style["meta"]), Paragraph("РЕКВИЗИТЫ", style["meta"])],
        [Paragraph("https://atlas.tvr.lat/", style["meta"]), Paragraph("ИНН 370266611106  /  ОГРНИП 322370000004857", style["meta"])],
    ], colWidths=[78 * mm, 78 * mm])
    metadata.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F4F8FB")),
        ("BOX", (0, 0), (-1, -1), .4, colors.HexColor("#D6E2EB")),
        ("INNERGRID", (0, 0), (-1, -1), .25, colors.HexColor("#DCE7EF")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    story += [metadata, Spacer(1, 8 * mm)]
    for heading, paragraphs in sections:
        story.append(Paragraph(heading, style["section"]))
        story.extend(Paragraph(text, style["body"]) for text in paragraphs)
    AtlasDocument(path, title=title, subject=subtitle).build(story)


def main() -> None:
    _register_fonts()
    documents = [
        ("atlas-public-offer.pdf", "Публичная оферта", "ПУБЛИЧНАЯ ОФЕРТА  /  ATLAS", "Условия оказания цифровой услуги Atlas и приобретения вычислительного резерва Atlas Token.", OFFER_SECTIONS),
        ("atlas-privacy-policy.pdf", "Политика обработки персональных данных", "ПОЛИТИКА ОБРАБОТКИ ПЕРСОНАЛЬНЫХ ДАННЫХ  /  ATLAS", "Порядок обработки и защиты персональных данных пользователей Atlas и магазина atlas.tvr.lat.", PRIVACY_SECTIONS),
    ]
    for filename, title, label, subtitle, sections in documents:
        output_path = OUTPUT_DIR / filename
        public_path = PUBLIC_DIR / filename
        _build(output_path, title=title, label=label, subtitle=subtitle, sections=sections)
        public_path.parent.mkdir(parents=True, exist_ok=True)
        copyfile(output_path, public_path)


if __name__ == "__main__":
    main()
