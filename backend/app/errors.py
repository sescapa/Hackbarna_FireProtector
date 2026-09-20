"""Turn database errors into HTTP responses that say what to do about them."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import Settings

logger = logging.getLogger(__name__)


def missing_table(settings: Settings) -> HTTPException:
    return HTTPException(
        status_code=503,
        detail=(
            f"Table '{settings.asset_specs_table}' with columns "
            f"'{settings.latitude_column}'/'{settings.longitude_column}' was not found "
            "in the database. Load the asset dataset, or point "
            "ASSET_SPECS_TABLE / LATITUDE_COLUMN / LONGITUDE_COLUMN at it."
        ),
    )


def _diagnostic(exc: psycopg.Error) -> str:
    """The primary message plus, when present, the constraint that failed."""
    parts = [exc.diag.message_primary or str(exc)]
    if exc.diag.constraint_name:
        parts.append(f"(constraint: {exc.diag.constraint_name})")
    if exc.diag.message_detail:
        parts.append(exc.diag.message_detail)
    return " ".join(parts)


@contextmanager
def translate_db_errors(settings: Settings) -> Iterator[None]:
    """Map psycopg failures onto meaningful status codes for both routes."""
    try:
        yield
    except (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn) as exc:
        logger.warning("asset data not queryable: %s", exc)
        raise missing_table(settings) from exc
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(status_code=409, detail=_diagnostic(exc)) from exc
    except (
        psycopg.errors.NotNullViolation,
        psycopg.errors.CheckViolation,
        psycopg.errors.ForeignKeyViolation,
        psycopg.errors.InvalidTextRepresentation,
        psycopg.errors.NumericValueOutOfRange,
        psycopg.errors.DatatypeMismatch,
        psycopg.errors.StringDataRightTruncation,
    ) as exc:
        raise HTTPException(status_code=400, detail=_diagnostic(exc)) from exc
    except psycopg.OperationalError as exc:
        logger.exception("database unavailable")
        raise HTTPException(status_code=503, detail=f"Database unavailable: {exc}") from exc


# ------------------------------------------------------------- envelope ----
# Every error the API returns uses the contract's shape:
#     {"error": {"code": "...", "message": "..."}}
# Installed application-wide, so the two older routes report errors the same
# way /assets does rather than leaking FastAPI's default {"detail": ...}.

_CODES = {
    400: "bad_request",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    422: "bad_request",
    # 502/504 come from /fire/arrival-grid (Open-Meteo upstream, ELMFIRE timeout). /assets
    # never returns them, so naming them does not touch the contract's 400/500.
    502: "upstream_error",
    503: "unavailable",
    504: "upstream_timeout",
}


def error_response(status_code: int, message: str) -> JSONResponse:
    code = _CODES.get(status_code, "internal_error")
    # A validation failure is a bad request whatever FastAPI calls it, and the
    # contract knows only 400 and 500.
    status = 400 if status_code == 422 else status_code
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


def _validation_message(exc: RequestValidationError) -> str:
    """FastAPI's error list as one sentence naming the parameters at fault."""
    parts = []
    for error in exc.errors():
        location = ".".join(str(piece) for piece in error.get("loc", ()) if piece != "body")
        parts.append(f"{location}: {error.get('msg', 'invalid')}" if location else error.get("msg", "invalid"))
    return "; ".join(parts) or "Request could not be validated."


def install_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return error_response(exc.status_code, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response(400, _validation_message(exc))

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        # Logged in full, reported as a generic message: an exception string can
        # carry connection details.
        logger.exception("unhandled error")
        return error_response(500, "Internal server error.")
