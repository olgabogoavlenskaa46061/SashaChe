"""Редакция: Claude выбирает темы дня и пишет сценарии роликов."""
from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

import config
from .collector import NewsItem

log = logging.getLogger(__name__)

AUDIENCE = (
    "Аудитория канала — русскоязычные и испаноязычные болельщики. Им интересны герои, которых знают все: "
    "«Реал», «Барселона», «Атлетико», топ-клубы АПЛ, ПСЖ, «Бавария», «Интер», «Милан», «Ювентус», "
    "«Бока» и «Ривер», сборные Испании, Аргентины, Бразилии, Португалии, Мексики, Франции; "
    "Месси, Роналду, Мбаппе, Ямаль, Винисиус, Холанд, Беллингем, Неймар и другие звёзды мирового уровня. "
    "Российский футбол — только если историю обсуждают все."
)
NO_NAMES = (
    "Не бери ноунеймов — игроков, тренеров и клубы, о которых эта аудитория не слышала "
    "(например, бывший тренер «Вулверхэмптона» на благотворительной акции — мимо). "
    "Исключение — сам момент настолько дикий, что его смотрят миллионы (гол через всё поле, "
    "вратарь-бомбардир, невероятный сейв): тогда главный герой — момент, а не имя."
)


def _count(n: int) -> str:
    from .trends import human_count
    return human_count(n)


def _stats(trend) -> str:
    from .trends import stats_line
    return stats_line(trend) or "просмотры неизвестны"


def _video_item(trend, related: list[NewsItem]) -> NewsItem:
    """Популярное видео в виде «сюжета»: заголовок — текст поста, источник — площадка,
    related — новости про этот момент (для фактов и ссылок)."""
    published = datetime.now(ZoneInfo("UTC"))
    if trend.published:
        try:
            published = datetime.fromisoformat(trend.published.replace("Z", "+00:00"))
        except ValueError:
            pass
    return NewsItem(id=f"video:{trend.url}", title=trend.title, summary=trend.description, link=trend.url,
                    source=trend.platform, lang="", published=published, related=list(related))


# Цены за 1 млн токенов (вход, выход) — только для примерной оценки в логе.
_PRICES = {"claude-sonnet-5": (2.0, 10.0), "claude-haiku-4-5-20251001": (1.0, 5.0)}


@dataclass
class Selection:
    item: NewsItem
    category: str
    why: str
    trends: list = field(default_factory=list)  # популярные видео про этот момент (trends.Trend)


@dataclass
class Script:
    category: str
    hook: str
    text: str
    title: str
    description: str
    hashtags: list[str]
    footage_queries: list[str]
    mood: str
    sources: list[tuple[str, str]] = field(default_factory=list)  # (название, ссылка)
    context: str = ""  # суть новости в двух словах — для того, кто читает текст
    popular: list[dict] = field(default_factory=list)  # популярные видео про этот момент (YouTube, X)

    @property
    def words(self) -> int:
        return len(self.text.split())

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in (
            "category", "hook", "text", "title", "description", "hashtags",
            "footage_queries", "mood", "sources", "context", "popular")}

    @classmethod
    def from_dict(cls, data: dict) -> "Script":
        data = dict(data)
        data["sources"] = [tuple(s) for s in data.get("sources", [])]
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class Editor:
    def __init__(self, client=None, model: str | None = None):
        if client is None:
            import anthropic
            if not config.ANTHROPIC_API_KEY:
                raise RuntimeError("Не задан ANTHROPIC_API_KEY")
            client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY, max_retries=4, timeout=180)
        self.client = client
        self.model = model or config.CLAUDE_MODEL
        self.tokens_in = 0
        self.tokens_out = 0

    def check_key(self) -> None:
        """Быстрая проверка ключа без расхода токенов."""
        self.client.models.list(limit=1)

    # ── общий вызов ──────────────────────────────────────────────────────
    def _ask(self, system: str, user: str | list, schema: dict, max_tokens: int = 3000,
             images: list[bytes] | None = None) -> dict:
        content: str | list = user
        if images and isinstance(user, str):  # кадры из видео — Claude видит, что происходит в моменте
            content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                    "data": base64.b64encode(img).decode("ascii")}}
                       for img in images] + [{"type": "text", "text": user}]
        response = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": content}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
        usage = getattr(response, "usage", None)
        if usage is not None:
            self.tokens_in += getattr(usage, "input_tokens", 0) or 0
            self.tokens_out += getattr(usage, "output_tokens", 0) or 0
        stop = getattr(response, "stop_reason", None)
        if stop == "refusal":
            raise RuntimeError("Claude отказался отвечать на этот запрос")
        text = "".join(getattr(block, "text", "") for block in response.content
                       if getattr(block, "type", "") == "text")
        if stop == "max_tokens" or not text.strip():
            # не уложился в лимит (например, долго думал над большим списком) — повторяем с запасом
            if max_tokens < 16000:
                log.warning("Claude не уложился в %d токенов (stop_reason=%s) — повторяю с запасом",
                            max_tokens, stop)
                return self._ask(system, user, schema, max_tokens=min(16000, max_tokens * 3), images=images)
            kinds = ", ".join(sorted({getattr(b, "type", "?") for b in response.content})) or "пусто"
            raise RuntimeError(f"Claude не вернул ответ (stop_reason={stop}, блоки: {kinds})")
        return json.loads(text)

    def cost_line(self) -> str:
        price = _PRICES.get(self.model)
        line = f"токены: {self.tokens_in} вход / {self.tokens_out} выход"
        if price:
            usd = self.tokens_in / 1e6 * price[0] + self.tokens_out / 1e6 * price[1]
            line += f" ≈ ${usd:.3f}"
        return line

    # ── 1. Выбор тем ─────────────────────────────────────────────────────
    def select(self, stories: list[NewsItem], mix: dict[str, int],
               recent_titles: list[str], max_items: int = 320, trends: list | None = None) -> list[Selection]:
        tz = ZoneInfo(config.TIMEZONE)
        trends = list(trends or [])
        trend_by_id = {t.id: t for t in trends}
        by_id = {s.id: s for s in stories[:max_items]}
        lines = []
        for story in stories[:max_items]:
            local = story.published.astimezone(tz)
            src = ", ".join(story.sources)
            count = f" ({len(story.sources)} ист.)" if len(story.sources) > 1 else ""
            summary = f" — {story.summary[:200]}" if story.summary else ""
            lines.append(f"[{story.id}] {local:%d.%m %H:%M} · {src}{count} · {story.title}{summary}")

        wanted = "\n".join(
            f"- {count} × «{name}»" for name, count in mix.items() if count > 0
        )
        total = sum(mix.values())
        recent = "\n".join(f"- {t}" for t in recent_titles[-40:]) or "- (пока ничего)"

        if config.VIDEO_STYLE == "sasha":
            intro = (f"Ты — шеф-редактор футбольного канала «{config.CHANNEL_NAME}» с короткими вертикальными видео. "
                     "Автор канала — Саша, болельщик и путешественник, который комментирует футбол своим голосом "
                     "и с иронией. Каждый день ты выбираешь из ленты новостей за сутки темы для его шортсов. "
                     "Лучше всего заходят свежие матчи и истории, над которыми можно жёстко пошутить: "
                     "провалы звёзд, скандалы, решения судей, громкие заявления, нелепые ситуации. "
                     + AUDIENCE + " " + NO_NAMES)
        else:
            intro = (f"Ты — шеф-редактор русскоязычного канала коротких вертикальных видео про футбол "
                     f"«{config.CHANNEL_NAME}». Каждый день ты выбираешь из ленты новостей за сутки темы для шортсов "
                     "(YouTube Shorts, TikTok, Reels).")
        system = f"""{intro}

Сегодня нужно выбрать {total} тем:
{wanted}

Что значат категории:
- «главное» — самые громкие события дня: неожиданные результаты, крупные трансферы, отставки тренеров, скандалы, решения, которые обсуждает весь футбольный мир.
- «курьёз» — смешное и нелепое: забавные ситуации на поле и вне поля, странные запреты и правила, розыгрыши, троллинг, нелепые ошибки, дерзкие или смешные цитаты, неожиданные поступки звёзд.
- «интересное» — то, что удивляет: рекорды и необычная статистика, редкие факты, трогательные истории, неожиданные совпадения, «а вы знали».

Как выбирать:
- Думай как зритель, который листает ленту: остановится ли он на этом? Звёзды и топ-клубы, драма, неожиданность, эмоции, повод поспорить в комментариях.
- Сюжет, о котором пишут несколько источников, обычно важнее. Английские источники тоже подходят — ролики будут на русском.
- Курьёзы часто прячутся в цитатах и мелких заметках — ищи их по всей ленте, а не только наверху.
- Не бери анонсы, трансляции, расписания, ставки, прогнозы и скучные «X прокомментировал Y» без изюминки.
- Не бери смерти, тяжёлые болезни, реанимацию, насилие, войну и политику — эти темы не для лёгкого автоматического формата. Обычные спортивные травмы — можно, но не как курьёз.
- Каждая тема — отдельный сюжет. Не бери две темы про одно и то же событие. Новости об одном матче — один сюжет, даже если они о разном (счёт, рекорд, слова тренера, гол звезды).
- Не повторяй сюжеты, которые уже выходили в последние дни (список ниже), в том числе под другим углом: если матч или событие уже было, новости о нём больше не бери. Вернуться к теме можно, только если случилось что-то новое и важное (например, скандал после матча или дисквалификация).
- Если в какой-то категории нет достойных тем, возьми вместо неё сильную тему другой категории и укажи её настоящую категорию.

Уже выходили:
{recent}

Верни темы в порядке от самой сильной к слабой. В поле id — ровно тот id, что в квадратных скобках."""
        if trends:
            system += """

Популярное за сутки. После ленты новостей — список самых просматриваемых футбольных видео за сутки на YouTube и в X. Это главный сигнал, что сейчас интересно зрителям:
- Если сюжет из ленты — это тот же момент, что и популярное видео (тот же гол, эпизод, выходка, заявление), бери такой сюжет в первую очередь, даже если о нём мало пишут.
- В поле trends перечисли id этих видео (например, t3) — только если это точно тот же момент. Если совпадений нет — пустой список.
- Видео, которому нет пары в ленте, темой быть не может: факты для текста берутся только из новостей.
- Хайлайты целых матчей, подборки, стримы и медиафутбол — слабый сигнал, в первую очередь смотри на отдельные яркие моменты."""

        schema = {
            "type": "object",
            "properties": {
                "stories": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "description": "id сюжета из квадратных скобок"},
                            "category": {"type": "string", "enum": list(config.CATEGORIES)},
                            "why": {"type": "string", "description": "одна фраза: чем зацепит зрителя"},
                        },
                        "required": ["id", "category", "why"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["stories"],
            "additionalProperties": False,
        }
        user = "Лента новостей за сутки (id · время по Москве · источники · заголовок — анонс):\n\n" + "\n".join(lines)
        if trends:
            item_schema = schema["properties"]["stories"]["items"]
            item_schema["properties"]["trends"] = {
                "type": "array", "items": {"type": "string"},
                "description": "id популярных видео (t1, t2…) про этот же момент; пусто, если таких нет"}
            item_schema["required"].append("trends")
            popular = []
            for t in trends:
                stats = " · ".join(x for x in (
                    f"{_count(t.views)} просмотров" if t.views else "",
                    f"{_count(t.likes)} лайков" if t.likes else "") if x)
                author = f" ({t.author})" if t.author else ""
                popular.append(f"[{t.id}] {t.platform} · {stats} · «{t.title}»{author}")
            user += ("\n\nПопулярное за сутки (id · площадка · просмотры · лайки · название или текст поста):\n\n"
                     + "\n".join(popular))
        data = self._ask(system, user, schema, max_tokens=2500)

        result: list[Selection] = []
        used: set[str] = set()
        for row in data.get("stories", []):
            item = by_id.get(str(row.get("id", "")).strip("[] "))
            if item is None or item.id in used:
                continue
            category = row.get("category") if row.get("category") in config.CATEGORIES else "главное"
            matched = []
            for trend_id in row.get("trends") or []:
                trend = trend_by_id.get(str(trend_id).strip("[] "))
                if trend is not None and trend not in matched:
                    matched.append(trend)
            result.append(Selection(item=item, category=category, why=row.get("why", ""), trends=matched))
            used.add(item.id)
            if len(result) >= total:
                break
        log.info("Выбрано тем: %d из %d (совпали с популярными видео: %d)", len(result), total,
                 sum(1 for r in result if r.trends))
        return result

    # ── 1а. Как снято видео: с трибун, трансляция или вообще не футбол ───
    def classify_footage(self, trends: list, thumbs: dict[str, bytes], batch: int = 50) -> dict[str, str]:
        """По обложкам: stands — матч по футболу, снятый с трибуны; broadcast — телетрансляция;
        other — не футбольный матч (американский футбол, другой спорт, студия, реклама…)."""
        system = """Ты смотришь обложки коротких видео и определяешь, что на каждой.
- stands — матч по обычному футболу (соккер), снятый зрителем с трибуны на телефон: в кадре зрители, головы и спины, ограждение или сетка, ракурс с трибуны, нет телевизионной графики.
- broadcast — телетрансляция матча по обычному футболу: табло со счётом и временем, логотип канала, телевизионный ракурс, повтор, крупный план с ТВ-камеры.
- other — всё остальное: американский футбол (шлемы, наплечники, овальный мяч), другие виды спорта, тренировка, интервью, студия, пресс-конференция, графика, реклама, селфи, раздевалка, съёмка из соцсетей игрока.
Если сомневаешься между stands и broadcast — выбирай broadcast."""
        schema = {
            "type": "object",
            "properties": {"videos": {"type": "array", "items": {
                "type": "object",
                "properties": {"id": {"type": "string"},
                               "kind": {"type": "string", "enum": ["stands", "broadcast", "other"]}},
                "required": ["id", "kind"], "additionalProperties": False}}},
            "required": ["videos"], "additionalProperties": False,
        }
        items = [t for t in trends if t.id in thumbs]
        labels: dict[str, str] = {}
        for start in range(0, len(items), batch):
            content: list = []
            for t in items[start:start + batch]:
                content.append({"type": "text", "text": f"[{t.id}] {t.platform}: {t.title[:120]}"})
                content.append({"type": "image", "source": {
                    "type": "base64", "media_type": "image/jpeg",
                    "data": base64.b64encode(thumbs[t.id]).decode("ascii")}})
            content.append({"type": "text", "text": "Определи kind для каждого видео выше. "
                                                    "id — ровно как в квадратных скобках."})
            data = self._ask(system, content, schema, max_tokens=4000)
            for row in data.get("videos", []):
                tid = str(row.get("id", "")).strip("[] ")
                if tid in thumbs and row.get("kind") in ("stands", "broadcast", "other"):
                    labels[tid] = row["kind"]
        return labels

    # ── 1б. Выбор тем из популярных видео (YouTube и X) ──────────────────
    def select_viral(self, trends: list, news: list[NewsItem], mix: dict[str, int],
                     recent_titles: list[str], max_news: int = 250,
                     used_links: set[str] | None = None, exact: bool = True,
                     stands_round: bool = False) -> list[Selection]:
        """Темы — самые популярные футбольные моменты за сутки. Новости — только чтобы сверить факты."""
        tz = ZoneInfo(config.TIMEZONE)
        trend_by_id = {t.id: t for t in trends}
        news_by_id = {n.id: n for n in news[:max_news]}
        wanted = "\n".join(f"- {count} × «{name}»" for name, count in mix.items() if count > 0)
        total = sum(mix.values())
        recent = "\n".join(f"- {t}" for t in recent_titles[-40:]) or "- (пока ничего)"

        videos = []
        for t in trends:
            when = ""
            if t.published:
                try:
                    when = datetime.fromisoformat(t.published.replace("Z", "+00:00")).astimezone(tz).strftime("%d.%m %H:%M")
                except ValueError:
                    pass
            if t.footage == "stands":
                extra = " · снято с трибун" + (", видео пойдёт в ролик" if t.video_url else "")
            elif t.footage == "broadcast":
                extra = " · телетрансляция"
            elif t.video_url and config.CLIP_SOURCE != "stands":
                extra = " · видео можно взять в ролик"
            else:
                extra = ""
            length = f" · {round(t.duration)} с" if t.duration else ""
            author = f" · {t.author}" if t.author else ""
            descr = f" — {t.description[:150]}" if t.description else ""
            videos.append(f"[{t.id}] {t.platform}{author} · {_stats(t)}{length}{extra} · {when} · «{t.title}»{descr}")
        used_links = used_links or set()
        headlines = [f"[{n.id}] {n.published.astimezone(tz):%d.%m %H:%M} · {', '.join(n.sources)} · {n.title}"
                     + (" · ⚠️ уже было в роликах" if any(i.link in used_links for i in n.all_items) else "")
                     for n in news[:max_news]]

        how_many = (f"Верни ровно {total} тем (не больше) в порядке от самой сильной к слабой." if exact else
                    f"Верни не больше {total} тем — только действительно достойные, лучше меньше, чем слабые. "
                    "Если достойных нет, верни пустой список. Порядок — от самой сильной к слабой.")
        if stands_round:
            clip_rule = ("- Здесь только видео, снятые болельщиками с трибун, — их и берём в ролик. Кроме моментов "
                         "матча подходит и жизнь трибун: баннеры и перформансы, кричалки, реакция фанатов на гол, "
                         "звезда у трибуны, выходки болельщиков — если клуб или игрок известны нашей аудитории "
                         "и матч свежий. Над таким тоже можно жёстко пошутить.")
        elif config.CLIP_SOURCE == "stands":
            clip_rule = ("- В ролик берём только видео, снятые болельщиками с трибун (пометка «снято с трибун»), "
                         "а не телетрансляции. В первую очередь бери моменты, у которых есть такое видео, и указывай "
                         "его главным, остальные видео про этот момент — в поле also. Момент только с телетрансляцией "
                         "бери, лишь если достойных моментов с трибун не хватает: тогда в ролике будут общие кадры стадиона.")
        else:
            clip_rule = ("- Если один и тот же момент есть на нескольких площадках, главным укажи видео с пометкой "
                         "«видео можно взять в ролик» (из X или Instagram), остальные — в поле also.")
        system = f"""Ты — шеф-редактор футбольного канала «{config.CHANNEL_NAME}» с короткими вертикальными видео. Автор — Саша, болельщик, который комментирует футбол своим голосом и с иронией. {AUDIENCE}

Каждый день ты выбираешь темы из самых популярных футбольных видео за сутки в X, Instagram и на YouTube: что люди смотрят и лайкают больше всего. Сегодня нужно {total} тем:
{wanted}

Что значат категории:
- «главное» — жёсткий или громкий момент матча: гол-шедевр, грубый фол, драка, удаление, решение судьи, провал звезды.
- «курьёз» — смешное и нелепое на поле и на трибунах: промахи с пяти метров, падения, нелепые голы, странные решения, выходки болельщиков во время матча.
- «интересное» — то, что удивляет: невероятная техника, финт, сейв, рекордный момент, редкость.

Как выбирать:
- Только моменты из свежих матчей — сыгранных за последние сутки-двое: с поля во время игры и сразу после финального свистка (празднование, стычка, реакция). Не бери тренировки, рекламу, закулисье, съёмки из соцсетей игроков, интервью вне матча, старые моменты («throwback», «on this day», «remember when») и подборки.
- Матч должен быть действительно свежим: сверяйся с лентой новостей. Если про этот матч за сутки нет новостей и по подписи не видно, что он вчерашний или сегодняшний, — не бери.
- Главное — чтобы над моментом можно было жёстко пошутить в стиле Саши: провал, нелепость, пафос, конфликт, судейский цирк, дорогая звезда в неловкой ситуации. Если момент просто красивый, а шутить не над чем, бери его, только если он совсем невероятный.
- Моменты всех лиг и турниров. Тема — один конкретный момент из видео, о котором Саша расскажет за 20 секунд. Чем больше просмотров и лайков, тем лучше, но важнее, чтобы момент был ярким и понятным.
- Герой должен быть понятен: из подписи, названия или новостей видно, кто это (игрок, клуб, матч). Если непонятно, кто в кадре и что за матч, — не бери.
- {NO_NAMES}
- Только обычный футбол (соккер). Американский и студенческий футбол (NFL, NCAA, college football, тачдаун, квотербек, шлемы и овальный мяч), регби и другие виды спорта не бери, даже если в подписи написано «football». Не бери хайлайты целых матчей, обзоры туров, подборки «топ-10», подкасты, стримы, рекламу, ставки и медиафутбол.
- Не бери смерти, тяжёлые травмы, насилие, войну и политику.
{clip_rule}
- Каждая тема — отдельный момент. Разные видео про один и тот же эпизод — одна тема.
- Не повторяй то, что уже выходило (список ниже). Разные эпизоды одного матча — это тот же сюжет: если матч уже был, никакие видео с него не бери (гол, сейв, празднование, раздевалка — всё это тот же матч). Новости с пометкой «уже было в роликах» — про такие матчи и события.
- Если в какой-то категории нет достойного момента, возьми сильный момент другой категории и укажи его настоящую категорию.

Факты:
- Ниже ещё лента новостей за сутки. Темой новость быть не может — она нужна только чтобы сверить факты. В поле news укажи id новостей про этот же момент или матч (счёт, кто забил, что случилось). Если таких нет — пустой список.

Уже выходили:
{recent}

{how_many} В поле trend — ровно тот id, что в квадратных скобках (например, t3)."""
        schema = {
            "type": "object",
            "properties": {
                "stories": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "trend": {"type": "string", "description": "id главного видео (t1, t2…)"},
                            "also": {"type": "array", "items": {"type": "string"},
                                     "description": "id других видео про этот же момент"},
                            "news": {"type": "array", "items": {"type": "string"},
                                     "description": "id новостей про этот момент — для проверки фактов"},
                            "category": {"type": "string", "enum": list(config.CATEGORIES)},
                            "why": {"type": "string", "description": "одна фраза: чем зацепит зрителя"},
                        },
                        "required": ["trend", "also", "news", "category", "why"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["stories"],
            "additionalProperties": False,
        }
        user = ("Популярные футбольные видео за сутки (id · площадка · автор · просмотры · лайки · длина · "
                "время по Москве · текст поста или название — описание):\n\n" + "\n".join(videos))
        if headlines:
            user += "\n\nЛента новостей за сутки — только для проверки фактов (id · время · источники · заголовок):\n\n"
            user += "\n".join(headlines)
        data = self._ask(system, user, schema, max_tokens=6000)

        result: list[Selection] = []
        used: set[str] = set()
        for row in data.get("stories", []):
            main = trend_by_id.get(str(row.get("trend", "")).strip("[] "))
            if main is None or main.id in used:
                continue
            others = []
            for trend_id in row.get("also") or []:
                other = trend_by_id.get(str(trend_id).strip("[] "))
                if other is not None and other is not main and other not in others and other.id not in used:
                    others.append(other)
            related = []
            for news_id in row.get("news") or []:
                item = news_by_id.get(str(news_id).strip("[] "))
                if item is not None and item not in related:
                    related.append(item)
            category = row.get("category") if row.get("category") in config.CATEGORIES else "главное"
            result.append(Selection(item=_video_item(main, related), category=category,
                                    why=row.get("why", ""), trends=[main] + others))
            used.update(t.id for t in [main] + others)
            if len(result) >= total:
                break
        log.info("Выбрано моментов из популярных видео: %d из %d (с новостями для фактов: %d)", len(result),
                 total, sum(1 for r in result if r.item.related))
        return result

    # ── 2. Сценарий ──────────────────────────────────────────────────────
    def _sasha_prompt(self, category: str, reader: str) -> str:
        tone = {
            "главное": "дерзко и с жёстким сарказмом — как болельщик, который всё видел и не стесняется в оценках",
            "курьёз": "с откровенным стёбом, как будто сам еле сдерживаешь смех",
            "интересное": "с удивлением и колкой усмешкой: «вы только посмотрите»",
        }[category]
        if reader == "human":
            reader_line = "Текст Саша прочитает сам своим голосом."
            reader_rules = ("- Цифры можно писать цифрами, счёт — как «2:1». В тексте без эмодзи, хештегов и скобок.\n"
                            "- Пиши так, чтобы легко читалось вслух с первого раза: без длинных имён подряд и "
                            "труднопроизносимых оборотов.")
        else:
            reader_line = "Текст прочитает синтезатор речи."
            reader_rules = ("- Текст читает синтезатор: числа, счёт и даты пиши словами («два — один», «сто четырнадцать»), "
                            "без сокращений («Лига чемпионов», а не «ЛЧ»), без эмодзи, хештегов и скобок.")
        return f"""Ты пишешь тексты для коротких видео футбольного канала «{config.CHANNEL_NAME}». Автор — Саша, болельщик, который ездит на матчи и комментирует футбол с иронией. {reader_line} Поверх пойдёт видео этого момента или кадры со стадиона, без надписей, в конце — замедленный повтор.

Как звучит Саша:
- Живая разговорная речь, как будто рассказываешь другу на трибуне. Можно «ну», «вот», «а какие варианты», но без перебора.
- Шути жёстко, как Саша: сарказм, дерзкие подколы, гиперболы, неожиданные сравнения, злая точность. Высмеивать можно игру и поступки: промахи, ошибки, пафос, симуляции, судейство, ценники трансферов, провалы клубов и тренеров. Без мата и без насмешек над внешностью, национальностью, травмами и бедой.
- Первая фраза — хук: очень интригующий и смешной вопрос, от которого невозможно пролистать. Он дразнит, но не раскрывает ответ. Например: «Сколько нужно промахнуться с пяти метров, чтобы тебя пожалел даже вратарь?», «Как стоить сто миллионов и проиграть дуэль газону?», «Что должно случиться, чтобы судья сам удивился своему свистку?». Никаких «Привет», «Сегодня», «Друзья», «В этом видео».
- Вопросы-хуки бывают разные — не начинай каждый со «Сколько»: «Что будет, если…», «Кто…», «Зачем…», «Угадайте, …», «Как…», «Почему…».
- Держи интригу до конца. Схема: хук-вопрос → что произошло (2–4 коротких предложения, ответ ещё не звучит) → ответ на вопрос из хука и колкая развязка — только в последней фразе. Зритель должен досмотреть, чтобы узнать ответ.
- Перечитай текст: без опечаток и ошибок в согласовании.
- Без «подписывайтесь» и «пишите в комментариях».
- Слушают русскоязычные и испаноязычные болельщики: героев называй так, чтобы было понятно, кто это («вингер «Реала» Винисиус»), но без лекций.
- {config.SCRIPT_MIN_WORDS}–{config.SCRIPT_MAX_WORDS} слов (20–25 секунд), короткие предложения.
- Тон для этой темы: {tone}.

Факты:
- Только из присланных материалов. Ничего не выдумывай: ни цифр, ни цитат, ни деталей, ни причин. Слухи подавай как слухи («пишут, что…», «по данным…»).
- Не добавляй фактов из своей памяти — годы, прошлые клубы, статистику, биографию. Даже если уверен: только то, что есть в материалах.
- Если тема — популярное видео из X или с YouTube, текст поста — это подпись автора, а не проверенный факт: счёт, имена и детали сверяй с новостями из материалов. Если новостей нет — говори только о том, что есть в подписи и видно на кадрах, без лишних подробностей.
- Своими словами, не копируй фразы из статей.
- Имена, клубы и турниры — как принято в русских спортивных СМИ, иностранные издания — по-русски («Экип», «Би-би-си»).
{reader_rules}

Остальные поля:
- context — 1–2 нейтральных предложения о том, что случилось, чтобы Саша понимал, о чём читает.
- hook — тема в 2–5 словах (для подписи и имени файла), без точки.
- title — название ролика в стиле канала: 1–3 слова с иронией, с маленькой буквы, без эмодзи и точки (например: «ни стыда», «сам виноват», «лучше переплатить», «гении», «что-то не так»).
- description — 1–2 предложения под видео, в конце «Источник: …» с названиями изданий (для видео из X или YouTube — площадка).
- hashtags — 4–6 хештегов без пробелов, строчными: первым #футбол, дальше клубы, игроки и турнир (например: #барселона #лалига #реалмадрид #мбаппе).
- footage_queries — 3 коротких запроса НА АНГЛИЙСКОМ для стоковых видео, как будто снято болельщиком с трибуны: стадион, трибуны, фанаты, игроки на поле издалека (например: "football stadium crowd from stands", "soccer match stadium night", "football fans cheering stands"). Без имён людей, клубов и брендов.
- mood — настроение (пригодится, если добавите музыку)."""

    def _news_prompt(self, category: str) -> str:
        tone = {
            "главное": "энергично и чётко, как спортивный ведущий, который сообщает главную новость дня",
            "курьёз": "с иронией и лёгким юмором, как друг, который пересказывает смешную историю; без оскорблений и насмешек над внешностью, происхождением или бедой",
            "интересное": "с искренним удивлением, как будто делишься фактом, от которого у тебя самого отвисла челюсть",
        }[category]
        return f"""Ты пишешь сценарии для футбольных шортсов на русском языке. Текст прочитает синтезатор речи, поверх пойдут крупные субтитры, фоном — нейтральные стоковые кадры.

Правила сценария:
1. Длина — {config.SCRIPT_MIN_WORDS}–{config.SCRIPT_MAX_WORDS} слов.
2. Первая фраза — крючок до 10 слов: самый неожиданный факт, интрига или вопрос. Не начинай с «Привет», «Сегодня», «Итак», «Друзья».
3. Дальше — суть коротко и живо: кто, что случилось, почему это важно или смешно. Предложения короткие, до 15 слов.
4. Финал — вопрос к зрителю или колкая фраза, чтобы захотелось написать комментарий.
5. Тон: {tone}.
6. Только факты из присланных материалов. Ничего не выдумывай: ни цифр, ни цитат, ни деталей, ни причин. Если материалов мало — пиши короче, но не фантазируй. Слухи и инсайды подавай как слухи («по данным…», «как пишет…»).
7. Пересказывай своими словами, не копируй фразы из статей. Цитаты — только очень короткие, лучше передай смысл.
8. Имена, клубы и турниры — в привычном для русских спортивных СМИ написании. Иностранные названия СМИ пиши по-русски («Экип», «Би-би-си»).
9. Текст читает синтезатор: все числа, счёт и даты пиши словами («два — один», «сорок один год», «сто четырнадцать»). Без сокращений («Лига чемпионов», а не «ЛЧ»), без эмодзи, хештегов, скобок и списков.

Остальные поля:
- context — 1–2 нейтральных предложения о том, что случилось.
- hook — надпись на экране, 2–5 слов, можно цифры и восклицание, без точки в конце. Она должна интриговать, но не врать.
- title — заголовок для публикации, до 80 символов, можно один эмодзи.
- description — 1–2 предложения для описания под видео и в конце «Источник: …» с названиями изданий.
- hashtags — 5–8 хештегов без пробелов: #футбол, #shorts и теги по теме (клуб, игрок, турнир).
- footage_queries — 3 коротких запроса НА АНГЛИЙСКОМ для поиска бесплатных стоковых видео под настроение сюжета (например: "soccer stadium night crowd", "barber shop haircut", "football on grass slow motion"). Без имён людей, клубов и брендов.
- mood — настроение музыки."""

    def write_script(self, selection: Selection, material: str, today: str, reader: str | None = None,
                     images: list[bytes] | None = None, other_hooks: list[str] | None = None) -> Script:
        """reader: human — текст читает Саша; tts — синтезатор речи."""
        reader = reader or ("human" if config.VOICE_MODE == "human" else "tts")
        if config.VIDEO_STYLE == "sasha":
            system = self._sasha_prompt(selection.category, reader)
        else:
            system = self._news_prompt(selection.category)

        user = f"""Дата: {today}
Категория: {selection.category}
Почему выбрали тему: {selection.why}

Материалы:
{material}"""

        schema = {
            "type": "object",
            "properties": {
                "context": {"type": "string"},
                "hook": {"type": "string"},
                "script": {"type": "string"},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "hashtags": {"type": "array", "items": {"type": "string"}},
                "footage_queries": {"type": "array", "items": {"type": "string"}},
                "mood": {"type": "string", "enum": ["энергично", "весело", "драматично", "вдохновляюще"]},
            },
            "required": ["context", "hook", "script", "title", "description", "hashtags", "footage_queries", "mood"],
            "additionalProperties": False,
        }
        if other_hooks:
            user += ("\n\nХуки других роликов сегодня: " + "; ".join(f"«{h}»" for h in other_hooks)
                     + ". Начни по-другому: другая форма вопроса и другое первое слово.")
        if images:
            user += ("\n\nПриложены кадры из видео по порядку. По ним видно, что происходит в моменте, — "
                     "опиши это своими словами. Людей по лицам не узнавай. Имя можно взять, только если оно "
                     "написано: в тексте материалов или на кадре (фамилия и номер на футболке, титры трансляции, "
                     "табло). Счёт, минуты и цифры — тоже только из текста материалов или с табло на кадре.")
        data = self._ask(system, user, schema, max_tokens=2000, images=images)

        # Слишком длинный текст — просим сократить один раз.
        if len(data["script"].split()) > config.SCRIPT_MAX_WORDS * 1.3:
            log.info("Сценарий длинный (%d слов), сокращаю", len(data["script"].split()))
            shorter = self._ask(
                system,
                user + "\n\nЧерновик получился слишком длинным. Вот он:\n" + data["script"]
                + f"\n\nСократи до {config.SCRIPT_MAX_WORDS} слов, сохранив начало, главные факты и финальную фразу.",
                schema, max_tokens=2000,
            )
            data = shorter

        hashtags = []
        for tag in data.get("hashtags", []):
            tag = "#" + tag.strip().lstrip("#").replace(" ", "")
            if len(tag) > 1 and tag.lower() not in {h.lower() for h in hashtags}:
                hashtags.append(tag)

        sources = []
        for item in selection.item.all_items[:4]:
            if (item.source, item.link) not in sources:
                sources.append((item.source, item.link))

        return Script(
            category=selection.category,
            hook=data["hook"].strip().rstrip("."),
            text=data["script"].strip(),
            title=data["title"].strip(),
            description=data["description"].strip(),
            hashtags=hashtags[:8],
            footage_queries=[q for q in data.get("footage_queries", []) if q.strip()][:4],
            mood=data.get("mood", "энергично"),
            sources=sources,
            context=data.get("context", "").strip(),
        )


def today_label() -> str:
    months = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
              "августа", "сентября", "октября", "ноября", "декабря"]
    now = datetime.now(ZoneInfo(config.TIMEZONE))
    return f"{now.day} {months[now.month - 1]} {now.year}"
