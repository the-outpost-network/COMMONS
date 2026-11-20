"""COMMONS web app."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import (
    admin,
    admin_bookable,
    admin_history,
    admin_resources,
    anchors,
    audit,
    auth,
    board,
    calendar_page,
    community,
    decision_log,
    feed,  # noqa: F401  (registers the feed writer on every session)
    forecast,
    governance,
    impact_page,
    internal,
    join,
    phone_calendar,
    proposals,
    reminders,
    security,
    textfmt,
)
from app.assistant import Assistant, JSONCompleter, SlidingWindowLimit, Weather
from app.auth import DB, MaybeSignedIn, message_page
from app.calendar import member_name
from app.config import Settings, check_secrets, load_settings, read_secret
from app.db import make_engine, make_sessionmaker, upgrade_database
from app.i18n import en
from app.llm import LLMClient
from app.matrix_admin import MatrixAdmin
from app.matrix_oauth import MatrixOAuth
from app.ratelimit import RateLimiter
from app.stream import EventHub
from app.weather import WeatherService

TEMPLATES = Path(__file__).parent / "templates"


STATIC = Path(__file__).parent / "static"
# Where the source code is published (AGPL section 13: offered to network users).
SOURCE_URL = "https://github.com/the-outpost-network/commons"


def create_app(
    settings: Settings | None = None,
    http: httpx.AsyncClient | None = None,
    require_secrets: bool = True,
    *,
    llm: JSONCompleter | None = None,
    internal_token: str | None = None,
    admin_client_secret: str | None = None,
    weather: Weather | None = None,
) -> FastAPI:
    """Tests pass `http` (fake Matrix), `llm` (fake model), `weather` and the secrets."""
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if require_secrets:
            check_secrets()  # refuse to start without the secrets mounted by Compose
        upgrade_database(engine)
        # Demo data and real data never share a running app.
        with app.state.sessionmaker() as db:
            refused = community.demo_refusal(
                settings.environment, engine.url.database, community.is_demo(db)
            )
        if refused:
            logging.getLogger("commons").error("%s", refused)
            raise SystemExit(f"COMMONS can't start: {refused}")
        # The decision-log signing key: generated on the first start, then only loaded.
        with app.state.sessionmaker() as db, db.begin():
            app.state.log_signer = decision_log.init_signer(db, settings.key_dir)
        try:
            community.seed_once(
                engine, settings.community_file, logging.getLogger("commons"), app.state.log_signer
            )
        except community.CommunityConfigError as e:
            # Stop with the readable message instead of a traceback.
            logging.getLogger("commons").error("%s", e)
            raise SystemExit(f"COMMONS can't start: {e}") from None
        app.state.templates.env.globals["community_name"] = community.community_name(engine)
        client = http or httpx.AsyncClient(timeout=10, follow_redirects=False)
        app.state.matrix = MatrixOAuth(settings, client)
        app.state.internal_token = (
            read_secret("internal_api_token") if require_secrets else internal_token
        )

        # Matrix admin access through MAS, if configured (Synapse trial).
        names = None
        if settings.matrix_admin_client_id:
            secret = (
                (lambda: read_secret("matrix_admin_client_secret"))
                if require_secrets
                else (lambda: admin_client_secret or "")
            )
            names = MatrixAdmin(settings, client, secret)
        app.state.matrix_admin = names

        # The local model; CPU inference is slow, so it gets its own long timeout.
        llm_http = None
        model = llm
        if model is None:
            llm_http = httpx.AsyncClient(follow_redirects=False)
            key = read_secret("llm_api_key") if require_secrets else ""
            model = LLMClient(llm_http, settings.llm_url, key)
        # Weather from SMHI, through the egress proxy, refreshed in the background.
        weather_http = None
        weather_task = None
        weather_source = weather
        if weather_source is None and settings.weather_latitude is not None:
            weather_http = httpx.AsyncClient(
                proxy=settings.egress_proxy or None, follow_redirects=False
            )
            service = WeatherService(settings, weather_http)
            weather_task = asyncio.create_task(service.run_forever())
            weather_source = service
        app.state.weather = weather_source

        app.state.llm = model
        # Taps on vote messages, per sender (the assistant has its own limit).
        app.state.tap_limit = SlidingWindowLimit(settings.tap_rate_per_minute)
        app.state.assistant = Assistant(
            settings, model, names, weather_source, signer=app.state.log_signer
        )
        app.state.event_hub.start(asyncio.get_running_loop())
        # Proposals close at the end of their window, checked every minute.
        closer_task = asyncio.create_task(
            governance.run_closer(
                app.state.sessionmaker,
                app.state.log_signer,
                tz=ZoneInfo(settings.timezone),
            )
        )
        forecast_task = None
        if settings.forecast_check_seconds > 0:
            forecast_task = asyncio.create_task(
                forecast.run_forever(
                    app.state.sessionmaker,
                    settings.forecast_check_seconds,
                    ZoneInfo(settings.timezone),
                )
            )
        # The daily ledger digest and chain-head anchor in the community room.
        anchor_task = None
        if settings.anchor_check_seconds > 0:
            anchor_task = asyncio.create_task(
                anchors.run_forever(
                    app.state.sessionmaker,
                    app.state.log_signer,
                    ZoneInfo(settings.timezone),
                    settings.anchor_check_seconds,
                )
            )
        reminder_task = None
        if settings.reminder_check_seconds > 0:
            reminder_task = asyncio.create_task(
                reminders.run_forever(app.state.sessionmaker, settings.reminder_check_seconds)
            )
        try:
            yield
        finally:
            closer_task.cancel()
            if anchor_task is not None:
                anchor_task.cancel()
            if reminder_task is not None:
                reminder_task.cancel()
            if forecast_task is not None:
                forecast_task.cancel()
            app.state.event_hub.stop()
            if weather_task is not None:
                weather_task.cancel()
            for owned in (weather_http, llm_http, None if http else client):
                if owned is not None:
                    await owned.aclose()

    app = FastAPI(
        title=en.APP_NAME, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    # Connects lazily; Alembic migrations run at startup. Tests pass the password in the URL.
    password = (lambda: read_secret("database_password")) if require_secrets else None
    engine = make_engine(settings.database_url, password)
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessionmaker = make_sessionmaker(engine)
    app.state.event_hub = EventHub()
    app.state.templates = Jinja2Templates(directory=TEMPLATES)  # autoescape is on for .html
    app.state.templates.env.globals["community_name"] = None
    app.state.templates.env.globals["en"] = en
    # Short member names everywhere ("ana" for @ana:…), and the source-code link
    # the AGPL asks us to offer network users (a plain link; nothing is fetched).
    app.state.templates.env.globals["short_name"] = member_name
    app.state.templates.env.globals["source_url"] = SOURCE_URL
    # Content versions prevent old CSS/JS from being paired with new templates.
    app.state.templates.env.globals["asset_versions"] = {
        name: hashlib.sha256((STATIC / name).read_bytes()).hexdigest()[:12]
        for name in ("app.css", "app.js", "snackbar.js")
    }
    app.state.templates.env.filters["num"] = textfmt.number
    app.state.templates.env.filters["ago"] = textfmt.ago
    # The stylesheet. Public, like any web page's CSS; nothing else lives there.
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    # CSRF check on every state change, then security headers on every response
    # (added last = outermost, so refusals get the headers too).
    app.add_middleware(security.CSRFMiddleware)
    app.add_middleware(security.SecurityHeadersMiddleware)
    tz = ZoneInfo(settings.timezone)
    # Web pages say "Today 08:27" / "Sat 14 Nov 08:27"; `stamp` is the exact
    # form (14.11.26 08:27) for tooltips and the decision log's details.
    app.state.templates.env.filters["local"] = lambda dt: textfmt.web_time(dt, tz)
    app.state.templates.env.filters["stamp"] = lambda dt: dt.astimezone(tz).strftime(
        "%d.%m.%y %H:%M"
    )
    app.state.pending_logins = auth.PendingLogins()
    app.state.fresh_invites = {}  # admin session -> new invite link, shown once
    app.state.login_limiter = RateLimiter(settings.login_rate_per_minute)
    app.state.fresh_calendar_links = {}  # session -> new phone-calendar link, shown once
    app.state.ics_limiter = RateLimiter(calendar_page.ICS_PER_MINUTE)
    phone_calendar.redact_access_log()
    app.state.log = logging.getLogger("commons")
    app.include_router(auth.router)
    app.include_router(internal.router)
    app.include_router(admin.router)
    app.include_router(admin_resources.router)
    app.include_router(admin_history.router)
    app.include_router(admin_bookable.router)
    app.include_router(board.router)
    app.include_router(proposals.router)
    app.include_router(calendar_page.router)
    app.include_router(impact_page.router)
    app.include_router(audit.router)
    app.include_router(join.router)

    @app.get("/health", include_in_schema=False)
    def health() -> dict[str, str]:
        """Public: report that the ASGI process is responding to container health checks."""
        return {"status": "ok"}

    @app.get("/")
    def home(request: Request, current: MaybeSignedIn, db: DB, logged: int | None = None):
        """Public: sign-in page; signed-in members see the village board."""
        if current is not None:
            return board.page(request, db, current, logged=logged)
        return app.state.templates.TemplateResponse(request, "home.html", {"current": None})

    # Starlette's class: it also covers unknown addresses (the router's 404), not
    # only the HTTPExceptions the routes raise.
    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        text = exc.detail if isinstance(exc.detail, str) else en.BAD_REQUEST
        if exc.status_code == 404:
            text = en.NOT_FOUND
        title = en.CSRF_FAILED_TITLE if text == en.CSRF_FAILED else None
        return message_page(request, text, exc.status_code, title)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> Response:
        return message_page(request, en.BAD_REQUEST, 400)

    return app


def __getattr__(name: str):
    # `uvicorn app.main:app` builds the app lazily, so importing this module
    # (e.g. in tests) doesn't read the environment or open a database.
    if name == "app":
        global app
        app = create_app()
        return app
    raise AttributeError(name)
