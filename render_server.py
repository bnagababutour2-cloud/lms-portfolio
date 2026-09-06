from flask import Flask, render_template, request, jsonify, redirect, url_for, session, Response
from dotenv import load_dotenv
from supabase import create_client
import os
import json
import io
import csv
import re
from datetime import datetime
import pandas as pd

load_dotenv()

# ------------------------------------------------------------
# Database connection
# ------------------------------------------------------------
# Render/production can use the Supabase PostgreSQL pooler through
# DATABASE_URL. This avoids relying on the *.supabase.co REST hostname,
# which may be unavailable on some networks. Local development can still
# use SUPABASE_URL + SUPABASE_KEY when DATABASE_URL is not configured.
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://gonhpxvlqicirkmpazvb.supabase.co").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "sb_secret_VacgZ-hWuCBmWhvpyzNqbg_qWfAiVIY").strip()
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://postgres.gonhpxkvlqcirkmpazvb:T7NpZeSWVaNzhM5w@aws-0-ap-northeast-1.pooler.supabase.com:5432/postgres").strip()


class _DBResponse:
    def __init__(self, data=None, count=None):
        self.data = data or []
        self.count = count


class _PostgresQuery:
    """Small compatibility layer for the Supabase query calls used below."""

    def __init__(self, db_url, table):
        self.db_url = db_url
        self.table = table
        self._op = "select"
        self._columns = "*"
        self._filters = []
        self._params = []
        self._limit = None
        self._offset = None
        self._payload = None

    def select(self, columns="*"):
        self._op = "select"
        self._columns = columns or "*"
        return self

    def ilike(self, column, value):
        self._filters.append(f'"{column}" ILIKE %s')
        self._params.append(value)
        return self

    def eq(self, column, value):
        self._filters.append(f'"{column}" = %s')
        self._params.append(value)
        return self

    def in_(self, column, values):
        vals = list(values or [])
        if not vals:
            self._filters.append("FALSE")
            return self
        self._filters.append(f'"{column}" IN ({", ".join(["%s"] * len(vals))})')
        self._params.extend(vals)
        return self

    def limit(self, count):
        self._limit = int(count)
        self._offset = None
        return self

    def range(self, start, end):
        """PostgREST-compatible inclusive row range: start..end."""
        start = int(start)
        end = int(end)
        if start < 0 or end < start:
            raise ValueError("Invalid range: start must be >= 0 and end must be >= start")
        self._offset = start
        self._limit = end - start + 1
        return self

    def insert(self, payload):
        self._op = "insert"
        self._payload = payload
        return self

    def update(self, payload):
        self._op = "update"
        self._payload = payload or {}
        return self

    def delete(self):
        self._op = "delete"
        return self

    def _where_sql(self):
        if not self._filters:
            return ""
        return " WHERE " + " AND ".join(self._filters)

    def execute(self):
        try:
            import psycopg2
            from psycopg2.extras import RealDictCursor, execute_values
        except ImportError as exc:
            raise RuntimeError("psycopg2-binary is required for DATABASE_URL mode") from exc

        allowed_tables = {"clients", "holdings"}
        if self.table not in allowed_tables:
            raise RuntimeError(f"Unsupported table: {self.table}")

        con = psycopg2.connect(self.db_url, connect_timeout=15)
        try:
            with con.cursor(cursor_factory=RealDictCursor) as cur:
                if self._op == "select":
                    sql = f"SELECT {self._columns} FROM \"{self.table}\"" + self._where_sql()
                    params = list(self._params)
                    if self._limit is not None:
                        sql += " LIMIT %s"
                        params.append(self._limit)
                    if self._offset is not None:
                        sql += " OFFSET %s"
                        params.append(self._offset)
                    cur.execute(sql, params)
                    rows = [dict(r) for r in cur.fetchall()]
                    return _DBResponse(rows)

                if self._op == "insert":
                    rows = self._payload if isinstance(self._payload, list) else [self._payload]
                    if not rows:
                        return _DBResponse([])
                    columns = list(rows[0].keys())
                    if not all(set(r.keys()) == set(columns) for r in rows):
                        raise RuntimeError("Insert rows must use the same columns")
                    col_sql = ", ".join(f'"{c}"' for c in columns)
                    sql = f'INSERT INTO "{self.table}" ({col_sql}) VALUES %s RETURNING *'
                    values = [tuple(r.get(c) for c in columns) for r in rows]
                    execute_values(cur, sql, values)
                    data = [dict(r) for r in cur.fetchall()]
                    con.commit()
                    return _DBResponse(data)

                if self._op == "update":
                    if not self._payload:
                        return _DBResponse([])
                    assignments = []
                    params = []
                    for c, v in self._payload.items():
                        assignments.append(f'"{c}" = %s')
                        params.append(v)
                    sql = f'UPDATE "{self.table}" SET {", ".join(assignments)}' + self._where_sql() + " RETURNING *"
                    params.extend(self._params)
                    cur.execute(sql, params)
                    data = [dict(r) for r in cur.fetchall()]
                    con.commit()
                    return _DBResponse(data)

                if self._op == "delete":
                    sql = f'DELETE FROM "{self.table}"' + self._where_sql() + " RETURNING *"
                    cur.execute(sql, list(self._params))
                    data = [dict(r) for r in cur.fetchall()]
                    con.commit()
                    return _DBResponse(data)

                raise RuntimeError(f"Unsupported operation: {self._op}")
        finally:
            con.close()


class _PostgresClient:
    def __init__(self, db_url):
        self.db_url = db_url

    def table(self, table):
        return _PostgresQuery(self.db_url, table)


supabase = None
supabase_config_error = None
if DATABASE_URL:
    supabase = _PostgresClient(DATABASE_URL)
    print("Database mode: PostgreSQL DATABASE_URL")
elif SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        print("Database mode: Supabase Data API")
    except Exception as exc:
        supabase_config_error = f"Supabase configuration error: {exc}"
else:
    supabase_config_error = "Configure DATABASE_URL or SUPABASE_KEY in the environment."

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "lms-local-test-secret")

last_trade_rows = []
last_upload_info = None

PRODUCT_MAP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "holding_products.json")


def _load_product_map():
    try:
        with open(PRODUCT_MAP_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_product_map(data):
    try:
        tmp = PRODUCT_MAP_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, PRODUCT_MAP_FILE)
    except Exception as exc:
        print("Product map save warning:", exc)


def _product_for_holding(h):
    product_map = _load_product_map()
    keys = [
        str(h.get("id") or "").strip(),
        "P:" + str(h.get("portfolio_id") or "").strip(),
    ]
    for key in keys:
        if key and key not in ("", "P:") and key in product_map:
            return str(product_map.get(key) or "").strip().upper() or "-"
    # Safe fallback when no separate Product has been recorded.
    return str(h.get("product") or "NORMAL").strip().upper() or "NORMAL"

# Supervisor accounts requested for the LMS local/admin portal.
# Password for each supervisor is the same as the Supervisor ID.
# Access is deliberately scoped by Client ID so one supervisor cannot view
# another supervisor's clients.
MAIN_ADMIN_ID = "admin"
MAIN_ADMIN_PASSWORD = "Adityatrishika123"

MAIN_ADMIN_ACCOUNT = {
    "password": MAIN_ADMIN_PASSWORD,
    "name": "Main Admin",
    "prefixes": [],
    "exact": [],
    "can_manage": True,
    "can_upload": True,
    "is_main_admin": True,
}

SUPERVISOR_ACCOUNTS = {
    "1304": {
        "password": "1304",
        "name": "1304 Supervisor",
        "prefixes": ["1304"],
        "exact": ["130100002", "130100030"],
        "can_manage": True,
        "can_upload": True,
    },
    "1312": {
        "password": "1312",
        "name": "1312 Supervisor",
        "prefixes": ["1312"],
        "exact": [],
        "can_manage": False,
        "can_upload": False,
    },
    "1313": {
        "password": "1313",
        "name": "1313 Supervisor",
        "prefixes": ["1313", "1314"],
        "exact": [],
        "can_manage": False,
        "can_upload": False,
    },
    "13": {
        "password": "13",
        "name": "13 Supervisor",
        # Supervisor 13 is scoped to Client IDs beginning with 13.
        # This keeps the account restricted to its 13-series clients while
        # allowing full portfolio management and daily uploads.
        "prefixes": ["13"],
        "exact": [],
        "can_manage": True,
        "can_upload": True,
    },
}


def _get_supervisor(supervisor_id):
    key = str(supervisor_id or "").strip()
    if key == MAIN_ADMIN_ID:
        return MAIN_ADMIN_ACCOUNT
    return SUPERVISOR_ACCOUNTS.get(key)


def _supervisor_can_view(supervisor_id, client_id):
    account = _get_supervisor(supervisor_id)
    if not account:
        return False
    if str(supervisor_id or "").strip() == MAIN_ADMIN_ID:
        return True
    cid = str(client_id or "").strip()
    if cid in account["exact"]:
        return True
    return any(cid.startswith(prefix) for prefix in account["prefixes"])


def _filter_supervisor_clients(supervisor_id, clients):
    return [c for c in clients if _supervisor_can_view(supervisor_id, c.get("client_id"))]


def _filter_supervisor_holdings(supervisor_id, holdings):
    return [h for h in holdings if _supervisor_can_view(supervisor_id, h.get("client_id"))]


def _require_supervisor(can_manage=False):
    supervisor_id = session.get("supervisor_id")
    account = _get_supervisor(supervisor_id)
    if not account:
        return None, redirect(url_for("login"))
    if can_manage and not account.get("can_manage"):
        return None, (jsonify({"success": False, "message": "This supervisor has view-only access."}), 403)
    return account, None


def _require_owner_or_supervisor(client_id=None, can_manage=True):
    """Authorize portfolio mutations for either an admin/supervisor or the logged-in client.

    A client may only mutate holdings belonging to that exact client account.
    A supervisor is restricted by its normal client scope.
    """
    if session.get("supervisor_id"):
        account, error = _require_supervisor(can_manage=can_manage)
        return ("supervisor", account, error)

    session_client = str(session.get("client_id") or "").strip()
    if not session_client:
        return None, None, (jsonify({"success": False, "message": "Login required."}), 401)
    if client_id is not None and str(client_id).strip().lower() != session_client.lower():
        return None, None, (jsonify({"success": False, "message": "You can only manage your own portfolio."}), 403)
    if supabase is None:
        return None, None, (jsonify({"success": False, "message": supabase_config_error}), 500)
    return ("client", {"client_id": session_client, "can_manage": True}, None)


def _clean_number(value, default=0.0):
    try:
        if pd.isna(value):
            return float(default)
        return float(value)
    except Exception:
        return float(default)


def _clean_client_id(value):
    if pd.isna(value):
        return ""
    text = str(value).strip()
    if re.fullmatch(r"\d+\.0", text):
        text = text[:-2]
    return text


def _is_fno(symbol, product):
    s = str(symbol or "").upper().strip()
    p = str(product or "").upper().strip()
    fno_names = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX")
    fno_product_words = ("F&O", "FNO", "FO", "NFO", "FUT", "FUTURE", "OPT", "OPTION", "NRML")
    return any(name in s for name in fno_names) or any(word in p for word in fno_product_words)


def _require_db():
    if supabase is None:
        return jsonify({"success": False, "message": supabase_config_error}), 500
    return None


def _load_all_clients():
    clients = []
    page_size = 1000
    start = 0
    while True:
        result = (supabase.table("clients")
                  .select("client_id,client_name,mobile,email,dob,password_hash,status")
                  .range(start, start + page_size - 1).execute())
        page = result.data or []
        clients.extend(page)
        if len(page) < page_size:
            break
        start += page_size
    return clients


def _load_all_holdings():
    rows_db = []
    page_size = 1000
    start = 0
    while True:
        result = (supabase.table("holdings").select("*")
                  .range(start, start + page_size - 1).execute())
        page = result.data or []
        rows_db.extend(page)
        if len(page) < page_size:
            break
        start += page_size

    rows = []
    for h in rows_db:
        qty = _clean_number(h.get("quantity"))
        buy_price = _clean_number(h.get("buy_price"))
        ltp = _clean_number(h.get("ltp"), buy_price)
        market_value = qty * ltp
        mtm = (ltp - buy_price) * qty
        rows.append({
            "id": h.get("id"),
            "portfolio_id": h.get("portfolio_id"),
            "client_id": h.get("client_id"),
            "symbol": h.get("symbol"),
            "product": _product_for_holding(h),
            "exchange": h.get("exchange") or "-",
            "qty": qty,
            "buy_price": buy_price,
            "ltp": ltp,
            "market_value": market_value,
            "mtm": mtm,
        })
    rows.sort(key=lambda x: x["mtm"], reverse=True)
    return rows


@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("index.html")
    user_id = (request.form.get("userid") or request.form.get("client_id") or "").strip()
    password = (request.form.get("password") or "").strip()
    if not user_id or not password:
        return render_template("index.html", error="Please enter ID and Password.")

    # Main Admin is a local application account and is checked first.
    # This keeps Main Admin login independent of the clients table.
    if user_id.lower() == MAIN_ADMIN_ID.lower() and password == MAIN_ADMIN_PASSWORD:
        session.clear()
        session["supervisor_id"] = MAIN_ADMIN_ID
        session["supervisor_name"] = MAIN_ADMIN_ACCOUNT["name"]
        session["is_main_admin"] = True
        return redirect(url_for("admin_portfolio"))

    # Supervisor/Admin login.
    supervisor = _get_supervisor(user_id)
    if supervisor and supervisor.get("password", "").lower() == password.lower():
        session.clear()
        session["supervisor_id"] = user_id
        session["supervisor_name"] = supervisor.get("name")
        session["is_main_admin"] = False
        return redirect(url_for("admin_portfolio"))

    if supabase is None:
        return render_template("index.html", error=supabase_config_error)

    try:
        result = (supabase.table("clients").select("client_id,password_hash,status")
                  .ilike("client_id", user_id).limit(1).execute())
        if not result.data:
            return render_template("index.html", error="Invalid Client/Supervisor ID or Password.")
        client = result.data[0]
        status = str(client.get("status") or "").lower()
        stored_password = str(client.get("password_hash") or "")
        if status and status != "active":
            return render_template("index.html", error="This account is not active.")
        if stored_password.lower() != password.lower():
            return render_template("index.html", error="Invalid Client/Supervisor ID or Password.")
        session.clear()
        session["client_id"] = client.get("client_id")
        return redirect(url_for("client_portfolio"))
    except Exception as exc:
        return render_template("index.html", error=f"Login error: {exc}")


@app.route("/client")
def client_portfolio():
    if supabase is None:
        return render_template("index.html", error=supabase_config_error)
    client_id = session.get("client_id")
    if not client_id or session.get("supervisor_id"):
        return redirect(url_for("login"))
    try:
        cr = (supabase.table("clients").select("client_id,client_name,mobile,email,dob,status")
              .ilike("client_id", client_id).limit(1).execute())
        if not cr.data:
            session.clear()
            return redirect(url_for("login"))
        client = cr.data[0]
        result = supabase.table("holdings").select("*").ilike("client_id", client_id).execute()
        holdings = []
        for h in (result.data or []):
            qty = _clean_number(h.get("quantity"))
            buy_price = _clean_number(h.get("buy_price"))
            ltp = _clean_number(h.get("ltp"), buy_price)
            holdings.append({
                "id": h.get("id"), "portfolio_id": h.get("portfolio_id"),
                "symbol": h.get("symbol"), "product": _product_for_holding(h), "exchange": h.get("exchange") or "-",
                "qty": qty, "buy_price": buy_price, "ltp": ltp,
                "market_value": qty * ltp, "mtm": (ltp - buy_price) * qty
            })
        holdings.sort(key=lambda x: x["mtm"], reverse=True)
        total_investment = sum(h["qty"] * h["buy_price"] for h in holdings)
        current_value = sum(h["market_value"] for h in holdings)
        overall_mtm = sum(h["mtm"] for h in holdings)
        return render_template("client_portfolio.html", client=client, holdings=holdings,
                               total_investment=total_investment, current_value=current_value,
                               overall_mtm=overall_mtm)
    except Exception as exc:
        return f"Client data error: {exc}", 500


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/admin")
def admin_portfolio():
    if supabase is None:
        return render_template("index.html", error=supabase_config_error)
    supervisor_id = session.get("supervisor_id")
    account = _get_supervisor(supervisor_id)
    if not account:
        return redirect(url_for("login"))
    try:
        clients = _filter_supervisor_clients(supervisor_id, _load_all_clients())
        holdings = _filter_supervisor_holdings(supervisor_id, _load_all_holdings())
        return render_template("admin.html", clients=clients, holdings=holdings,
                               supervisor_id=supervisor_id, supervisor_name=account.get("name"),
                               can_manage=account.get("can_manage", False),
                               can_upload=account.get("can_upload", False))
    except Exception as exc:
        return f"Admin data error: {exc}", 500


# -------------------------
# Daily trade report
# -------------------------

def _prepare_holdings(rows):
    """Convert the daily report into the NEW current portfolio snapshot.

    Only strictly positive open Net Qty rows become holdings.
    Zero/negative Net Qty rows remain visible in the report preview but are
    NEVER inserted into holdings. This is important for sell-only rows such
    as Client 1201N99, where Net Qty can be -100 after a complete sale.
    """
    holdings, display_rows = [], []
    for row in rows:
        client_id = _clean_client_id(row.get("CId"))
        symbol = str(row.get("Symbol") or "").strip().upper()
        product = str(row.get("Prod") or "").strip().upper()
        if not client_id or not symbol:
            continue

        buy_qty = _clean_number(row.get("BQty"))
        sell_qty = _clean_number(row.get("SQty"))
        report_nqty = row.get("NQty")
        net_qty = buy_qty - sell_qty if pd.isna(report_nqty) else _clean_number(report_nqty)

        report_navg = row.get("NAvg")
        buy_avg = (_clean_number(report_navg)
                   if not pd.isna(report_navg) and _clean_number(report_navg) != 0
                   else _clean_number(row.get("BAvg")))

        # Keep every meaningful report row for the preview, including sold/closed
        # rows. They are report history, not active portfolio holdings.
        display_rows.append({
            "date": row.get("Date"), "client_id": client_id, "symbol": symbol, "product": product,
            "buy_qty": buy_qty, "buy_avg": buy_avg, "sell_qty": sell_qty,
            "sell_avg": _clean_number(row.get("SAvg")), "net_qty": net_qty, "net_avg": buy_avg,
            "pnl": _clean_number(row.get("Total Profit/Loss"))
        })

        # CRITICAL: negative or zero positions are closed/not-open positions.
        # Never create a negative holding in Supabase.
        if net_qty <= 0:
            continue

        # F&O/index positions: LTP = Buy Price. Equity starts at Buy Price until a live LTP feed is connected.
        ltp = buy_avg
        exchange = "NSE" if _is_fno(symbol, product) else "BSE"
        holdings.append({
            "client_id": client_id, "symbol": symbol, "exchange": exchange,
            "quantity": net_qty, "buy_price": buy_avg, "ltp": ltp,
            "market_value": net_qty * ltp, "pnl": (ltp - buy_avg) * net_qty,
            "__product": product
        })
    return holdings, display_rows


def _read_trade_excel(uploaded):
    workbook = pd.ExcelFile(uploaded)
    dataframe = None
    sheet_name = None
    for sheet in workbook.sheet_names:
        candidate = pd.read_excel(workbook, sheet_name=sheet)
        if not candidate.empty:
            dataframe, sheet_name = candidate, sheet
            break
    if dataframe is None:
        raise ValueError("The Excel file does not contain any data rows.")

    original_columns = list(dataframe.columns)
    normalized = {str(c).strip().lower().replace(" ", "").replace("_", ""): c for c in original_columns}
    aliases = {
        "cid": ["cid", "clientid"], "symbol": ["symbol"], "prod": ["prod", "product"],
        "bqty": ["bqty", "buyqty", "buyquantity"], "bavg": ["bavg", "buyavg", "buyprice"],
        "sqty": ["sqty", "sellqty", "sellquantity"], "savg": ["savg", "sellavg", "sellprice"],
        "nqty": ["nqty", "netqty", "netquantity"], "navg": ["navg", "netavg"], "date": ["date"],
        "pnl": ["totalprofit/loss", "totalprofitloss", "p/l", "pl", "profitloss"]
    }
    column_map = {}
    for logical, choices in aliases.items():
        for alias in choices:
            if alias in normalized:
                column_map[logical] = normalized[alias]
                break
    missing = [x for x in ["cid", "symbol", "prod", "bqty", "bavg"] if x not in column_map]
    if missing:
        raise ValueError("Missing required columns: " + ", ".join(missing) + ". Found: " + ", ".join(map(str, original_columns)))

    internal = pd.DataFrame()
    for logical in aliases:
        internal[logical] = dataframe[column_map[logical]] if logical in column_map else pd.NA
    internal = internal.rename(columns={
        "cid": "CId", "symbol": "Symbol", "prod": "Prod", "bqty": "BQty", "bavg": "BAvg",
        "sqty": "SQty", "savg": "SAvg", "nqty": "NQty", "navg": "NAvg", "date": "Date", "pnl": "Total Profit/Loss"
    })
    return sheet_name, internal.to_dict("records")


@app.route("/trade-report")
def trade_report():
    return render_template("trade_report.html", trade_rows=last_trade_rows, upload_info=last_upload_info)



def _daily_bucket(product: str) -> str:
    """Daily matching bucket: ONLY MTF is separate; everything else is NORMAL."""
    p = str(product or "").strip().upper()
    if p == "MTF":
        return "MTF"
    return "NORMAL"


def _daily_number(value, field: str, row_number: int, allow_blank=True) -> float:
    try:
        if value is None or (pd.isna(value) and not isinstance(value, str)) or str(value).strip() == "":
            if allow_blank:
                return 0.0
            raise ValueError(f"Row {row_number}: missing {field}")
    except TypeError:
        pass
    try:
        number = float(value)
    except Exception as exc:
        raise ValueError(f"Row {row_number}: invalid {field}: {value!r}") from exc
    if number < 0:
        raise ValueError(f"Row {row_number}: negative {field}: {number}")
    return number


def _daily_parse_trades(raw_rows):
    """
    Adapt the legacy V3 Daily Trade Updater rules to Supabase.

    Important: the broker file is a daily summary and has no Portfolio ID.
    Therefore Daily Trade SELL matching is Client + Symbol + Product Bucket,
    exactly like the supplied legacy updater.
    """
    trades = []
    for row_number, row in enumerate(raw_rows, start=2):
        client = _clean_client_id(row.get("CId"))
        symbol = str(row.get("Symbol") or "").strip().upper()
        product = str(row.get("Prod") or "").strip().upper()
        if not client or not symbol or not product:
            raise ValueError(f"Daily report row {row_number}: missing Client/Symbol/Product.")

        bucket = _daily_bucket(product)
        buy_qty = _daily_number(row.get("BQty"), "BQty", row_number)
        sell_qty = _daily_number(row.get("SQty"), "SQty", row_number)
        buy_price_raw = row.get("BAvg")
        sell_price_raw = row.get("SAvg")

        if buy_qty > 0 and (buy_price_raw is None or str(buy_price_raw).strip() == ""):
            raise ValueError(f"Daily report row {row_number}: BUY quantity exists but BAvg is missing.")
        if sell_qty > 0 and (sell_price_raw is None or str(sell_price_raw).strip() == ""):
            raise ValueError(f"Daily report row {row_number}: SELL quantity exists but SAvg is missing.")

        buy_price = float(buy_price_raw) if buy_price_raw not in (None, "") else None
        sell_price = float(sell_price_raw) if sell_price_raw not in (None, "") else None

        trades.append({
            "source_row": row_number,
            "date": row.get("Date"),
            "client": client,
            "symbol": symbol,
            "product": product,
            "bucket": bucket,
            "buy_qty": buy_qty,
            "buy_price": buy_price,
            "sell_qty": sell_qty,
            "sell_price": sell_price,
        })

    if not trades:
        raise ValueError("Daily report contains no trade data.")
    return trades


def _holding_stored_product(h):
    """Return product only when it is actually stored, else None."""
    direct = h.get("product")
    if direct not in (None, ""):
        return str(direct).strip().upper()
    pm = _load_product_map()
    for key in (
        str(h.get("id") or "").strip(),
        "P:" + str(h.get("portfolio_id") or "").strip(),
    ):
        if key and key != "P:" and key in pm:
            value = str(pm.get(key) or "").strip().upper()
            if value:
                return value
    return None


def _daily_prepare_display(raw_rows):
    """Create the report preview, including zero/negative net-flow rows."""
    display_rows = []
    for row in raw_rows:
        client_id = _clean_client_id(row.get("CId"))
        symbol = str(row.get("Symbol") or "").strip().upper()
        raw_product = str(row.get("Prod") or "").strip().upper()
        product = "MTF" if raw_product == "MTF" else ("NORMAL" if raw_product else "")
        buy_qty = _clean_number(row.get("BQty"))
        sell_qty = _clean_number(row.get("SQty"))
        net_qty = buy_qty - sell_qty
        buy_avg = _clean_number(row.get("BAvg"))
        if not client_id or not symbol:
            continue
        display_rows.append({
            "date": row.get("Date"),
            "client_id": client_id,
            "symbol": symbol,
            "product": product,
            "buy_qty": buy_qty,
            "buy_avg": buy_avg,
            "sell_qty": sell_qty,
            "sell_avg": _clean_number(row.get("SAvg")),
            "net_qty": net_qty,
            "net_avg": buy_avg if buy_avg else 0.0,
            "pnl": _clean_number(row.get("Total Profit/Loss")),
        })
    return display_rows


def _daily_build_result(existing_rows, trades):
    """
    Compute the final active portfolio in memory before any Supabase write.

    Legacy rules reproduced:
      * BUY creates a new lot.
      * Same Client+Symbol+Bucket+BUY PRICE BUYs are aggregated into one new lot.
      * Different BUY prices remain separate lots.
      * SELL ignores Portfolio ID because broker report has none.
      * SELL consumes matching lots by LOWEST BUY PRICE FIRST.
      * Different Client / Symbol / Bucket never cross.
      * SELL with no matching lot is ignored.
      * Excess SELL is ignored.
      * Zero quantity rows are deleted/not retained.
    """
    # Determine whether the existing row's product is explicit.
    pair_buckets = {}
    for trade in trades:
        pair_buckets.setdefault((trade["client"], trade["symbol"]), set()).add(trade["bucket"])

    lots = []
    for h in existing_rows:
        qty = _clean_number(h.get("quantity"))
        if qty <= 1e-9:
            continue
        client = _clean_client_id(h.get("client_id"))
        symbol = str(h.get("symbol") or "").strip().upper()
        stored_product = _holding_stored_product(h)
        if stored_product:
            bucket = _daily_bucket(stored_product)
            # Daily Portfolio Product is intentionally normalized:
            # ONLY MTF is distinct; every other product is displayed/stored as NORMAL.
            product = bucket
        else:
            candidate = pair_buckets.get((client, symbol), set())
            # Legacy updater backfills only when unambiguous; otherwise it leaves
            # the legacy blank bucket effectively NORMAL.
            bucket = next(iter(candidate)) if len(candidate) == 1 else "NORMAL"
            product = bucket
        lots.append({
            "db": True,
            "id": h.get("id"),
            "portfolio_id": h.get("portfolio_id"),
            "client": client,
            "symbol": symbol,
            "bucket": bucket,
            "product": product,
            "qty": qty,
            "buy_price": _clean_number(h.get("buy_price")),
            "ltp": _clean_number(h.get("ltp"), _clean_number(h.get("buy_price"))),
            "market_value": _clean_number(h.get("market_value")),
            "pnl": _clean_number(h.get("pnl")),
            "delete": False,
            "changed": False,
        })

    stats = {
        "trades_read": len(trades),
        "buy_rows": sum(t["buy_qty"] > 0 for t in trades),
        "sell_rows": sum(t["sell_qty"] > 0 for t in trades),
        "net_zero": 0,
        "lots_created": 0,
        "sell_allocations": 0,
        "partial_closes": 0,
        "fully_closed": 0,
        "rows_deleted": 0,
        "sell_excess_ignored": 0,
        "sell_ignored_no_buy": 0,
        "realised": 0.0,
    }

    aggregated_buys = {}
    sell_flows = []

    # EXACT LEGACY ORDER: first understand every row as a net flow,
    # then create/aggregate BUY lots, then process SELL flows.
    for trade in trades:
        buy_qty = trade["buy_qty"]
        sell_qty = trade["sell_qty"]
        net = buy_qty - sell_qty

        if abs(net) < 1e-9:
            if buy_qty > 0:
                stats["net_zero"] += 1
            continue

        if net > 0:
            price_key = round(float(trade["buy_price"]), 10)
            key = (trade["client"], trade["symbol"], trade["bucket"], price_key)
            if key not in aggregated_buys:
                aggregated_buys[key] = {
                    "client": trade["client"],
                    "symbol": trade["symbol"],
                    "bucket": trade["bucket"],
                    "product": trade["bucket"],
                    "qty": float(net),
                    "buy_price": float(trade["buy_price"]),
                    "date": trade["date"],
                    "source_rows": [trade["source_row"]],
                }
            else:
                aggregated_buys[key]["qty"] += float(net)
                aggregated_buys[key]["source_rows"].append(trade["source_row"])
        else:
            sell_flows.append(trade | {"effective_sell_qty": -net})

    # BUYs are created BEFORE SELLs, so same-day BUY + SELL can interact.
    new_lots = []
    for buy in aggregated_buys.values():
        new_lots.append({
            "db": False,
            "id": None,
            "portfolio_id": None,  # DB generates the authoritative Portfolio ID.
            "client": buy["client"],
            "symbol": buy["symbol"],
            "bucket": buy["bucket"],
            "product": buy["product"],
            "qty": float(buy["qty"]),
            "buy_price": float(buy["buy_price"]),
            "ltp": float(buy["buy_price"]),
            "market_value": float(buy["qty"]) * float(buy["buy_price"]),
            "pnl": 0.0,
            "delete": False,
            "changed": False,
            "buy_date": buy["date"],
            "source_rows": buy["source_rows"],
        })
        stats["lots_created"] += 1

    lots.extend(new_lots)

    # SELL: lowest BUY price first, never FIFO and never Portfolio-ID matching.
    for trade in sell_flows:
        remaining = float(trade["effective_sell_qty"])
        while remaining > 1e-9:
            eligible = [
                lot for lot in lots
                if not lot["delete"]
                and lot["client"] == trade["client"]
                and lot["symbol"] == trade["symbol"]
                and lot["bucket"] == trade["bucket"]
                and lot["qty"] > 1e-9
            ]
            if not eligible:
                stats["sell_ignored_no_buy"] += 1
                stats["sell_excess_ignored"] += 1
                break

            eligible.sort(key=lambda x: (x["buy_price"], str(x.get("buy_date") or ""), str(x.get("id") or "")))
            lot = eligible[0]
            allocated = min(remaining, lot["qty"])
            old_qty = lot["qty"]
            lot["qty"] = old_qty - allocated
            lot["changed"] = True
            stats["sell_allocations"] += 1
            sell_price = float(trade["sell_price"] or 0)
            stats["realised"] += (sell_price - lot["buy_price"]) * allocated

            if lot["qty"] <= 1e-9:
                lot["qty"] = 0.0
                lot["delete"] = True
                stats["fully_closed"] += 1
                if lot["db"]:
                    stats["rows_deleted"] += 1
            else:
                stats["partial_closes"] += 1

            remaining -= allocated

        if remaining > 1e-9:
            stats["sell_excess_ignored"] += 0 if stats["sell_excess_ignored"] else 1

    # Final safety: zero/negative quantities are never active.
    final_lots = [lot for lot in lots if not lot["delete"] and lot["qty"] > 1e-9]
    for lot in final_lots:
        if lot["db"]:
            if lot["ltp"] is None:
                lot["ltp"] = lot["buy_price"]
            lot["market_value"] = lot["qty"] * lot["ltp"]
            lot["pnl"] = (lot["ltp"] - lot["buy_price"]) * lot["qty"]

    return final_lots, lots, stats


def _daily_product_write_supported():
    """Check whether the optional product column exists in Supabase."""
    try:
        supabase.table("holdings").select("product").limit(1).execute()
        return True
    except Exception:
        return False


def _daily_insert_lots(new_lots):
    """Insert newly created lots and preserve Product/Mode when possible."""
    if not new_lots:
        return []
    product_supported = _daily_product_write_supported()
    inserted = []
    product_map = _load_product_map()

    for start in range(0, len(new_lots), 100):
        batch_lots = new_lots[start:start + 100]
        batch = []
        for lot in batch_lots:
            row = {
                "client_id": lot["client"],
                "symbol": lot["symbol"],
                "exchange": "NSE" if _is_fno(lot["symbol"], lot["product"]) else "BSE",
                "quantity": lot["qty"],
                "buy_price": lot["buy_price"],
                "ltp": lot["ltp"],
                "market_value": lot["market_value"],
                "pnl": lot["pnl"],
            }
            if product_supported:
                row["product"] = lot["product"]
            batch.append(row)

        result = supabase.table("holdings").insert(batch).execute()
        saved = result.data or []
        if len(saved) != len(batch):
            raise RuntimeError(f"Daily BUY insert verification failed: expected {len(batch)}, saved {len(saved)}.")
        inserted.extend(saved)

        for saved_row, lot in zip(saved, batch_lots):
            sid = str(saved_row.get("id") or "").strip()
            pid = str(saved_row.get("portfolio_id") or "").strip()
            if sid:
                product_map[sid] = lot["product"]
            if pid:
                product_map["P:" + pid] = lot["product"]

    _save_product_map(product_map)
    return inserted


def _daily_normalize_existing_products(lots):
    """Normalize active holding Product after a daily update.

    Business rule: ONLY MTF is distinct. Every other broker product label
    (CNC, MARGIN, DELIVERY, INTRADAY, etc.) is stored/displayed as NORMAL.
    """
    product_supported = _daily_product_write_supported()
    pm = _load_product_map()
    changed_map = False
    for lot in lots:
        if not lot.get("db") or not lot.get("id") or lot.get("delete"):
            continue
        normalized = "MTF" if lot.get("bucket") == "MTF" else "NORMAL"
        if product_supported:
            supabase.table("holdings").update({"product": normalized}).eq("id", lot["id"]).execute()
        sid = str(lot.get("id") or "").strip()
        pid = str(lot.get("portfolio_id") or "").strip()
        if sid:
            pm[sid] = normalized
            changed_map = True
        if pid:
            pm["P:" + pid] = normalized
            changed_map = True
    if changed_map:
        _save_product_map(pm)


def _daily_update_partial_lot(lot):
    """Update only the remaining quantity for a partially sold lot."""
    if not lot["db"] or not lot.get("id"):
        return
    qty = float(lot["qty"])
    ltp = float(lot.get("ltp") or lot["buy_price"])
    data = {
        "quantity": qty,
        # Buy price MUST remain unchanged.
        "buy_price": float(lot["buy_price"]),
        "ltp": ltp,
        "market_value": qty * ltp,
        "pnl": (ltp - float(lot["buy_price"])) * qty,
    }
    supabase.table("holdings").update(data).eq("id", lot["id"]).execute()


def _daily_delete_lots(lots):
    ids = [lot.get("id") for lot in lots if lot.get("db") and lot.get("delete") and lot.get("id") is not None]
    for start in range(0, len(ids), 100):
        batch = ids[start:start + 100]
        if batch:
            supabase.table("holdings").delete().in_("id", batch).execute()

    if ids:
        pm = _load_product_map()
        changed = False
        for sid in ids:
            sid = str(sid)
            if sid in pm:
                pm.pop(sid, None)
                changed = True
        _save_product_map(pm)


def _daily_restore_snapshot(old_rows, old_product_map):
    """Best-effort rollback for Supabase mutations."""
    try:
        current = supabase.table("holdings").select("id").execute().data or []
        ids = [r.get("id") for r in current if r.get("id") is not None]
        for start in range(0, len(ids), 100):
            batch = ids[start:start + 100]
            if batch:
                supabase.table("holdings").delete().in_("id", batch).execute()

        restore = []
        product_supported = _daily_product_write_supported()
        for row in old_rows:
            clean = {k: v for k, v in row.items() if k != "id" and k != "created_at"}
            if not product_supported:
                clean.pop("product", None)
            restore.append(clean)
        for start in range(0, len(restore), 100):
            batch = restore[start:start + 100]
            if batch:
                supabase.table("holdings").insert(batch).execute()
        _save_product_map(old_product_map)
    except Exception as rollback_error:
        print("CRITICAL: daily portfolio rollback failed:", rollback_error)


@app.route("/api/trade-report/upload", methods=["POST"])
def upload_trade_report():
    """Process one complete daily trade snapshot and always return JSON.

    This is an API endpoint, so authentication/database failures must never
    return an HTML redirect. Keeping the whole handler inside the try block
    also prevents an unexpected exception from reaching the browser as an
    empty/non-JSON response.
    """
    global last_trade_rows, last_upload_info

    try:
        # API endpoints must return JSON instead of redirecting to /login.
        supervisor_id = session.get("supervisor_id")
        account = _get_supervisor(supervisor_id)
        if not account:
            return jsonify({
                "success": False,
                "message": "Your supervisor session has expired. Please log in again.",
                "code": "AUTH_REQUIRED",
            }), 401

        if not account.get("can_manage"):
            return jsonify({
                "success": False,
                "message": "This supervisor has view-only access.",
                "code": "VIEW_ONLY",
            }), 403

        if not account.get("can_upload"):
            return jsonify({
                "success": False,
                "message": "Daily report upload is restricted to an authorized supervisor.",
                "code": "UPLOAD_NOT_ALLOWED",
            }), 403

        if supabase is None:
            return jsonify({
                "success": False,
                "message": supabase_config_error or "Database is not configured.",
                "code": "DATABASE_NOT_CONFIGURED",
            }), 500

        uploaded = request.files.get("file")
        if uploaded is None or not uploaded.filename:
            return jsonify({
                "success": False,
                "message": "Please choose the daily Excel report.",
                "code": "FILE_REQUIRED",
            }), 400

        filename = os.path.basename(uploaded.filename)
        if not filename.lower().endswith((".xlsx", ".xls")):
            return jsonify({
                "success": False,
                "message": "Please upload an Excel file (.xlsx or .xls).",
                "code": "INVALID_FILE_TYPE",
            }), 400

        sheet_name, raw_rows = _read_trade_excel(uploaded)
        trades = _daily_parse_trades(raw_rows)
        display_rows = _daily_prepare_display(raw_rows)

        # Canonicalize Client IDs case-insensitively BEFORE any database mutation.
        all_clients = _load_all_clients()
        client_map = {
            str(c.get("client_id") or "").strip().lower(): c.get("client_id")
            for c in all_clients if c.get("client_id")
        }
        unknown = []
        for trade in trades:
            canonical = client_map.get(trade["client"].lower())
            if not canonical:
                unknown.append(trade["client"])
            else:
                trade["client"] = canonical
        for display in display_rows:
            canonical = client_map.get(str(display["client_id"]).strip().lower())
            if canonical:
                display["client_id"] = canonical

        if unknown:
            unknown = sorted(set(unknown))
            return jsonify({
                "success": False,
                "message": "Unknown Client IDs: " + ", ".join(unknown[:30]),
                "unknown_clients": unknown,
            }), 400

        # Snapshot current portfolio before computing/applying daily changes.
        old_rows = supabase.table("holdings").select("*").execute().data or []
        old_product_map = _load_product_map()

        final_lots, all_lots, stats = _daily_build_result(old_rows, trades)

        delete_lots = [lot for lot in all_lots if lot.get("db") and lot.get("delete")]
        partial_lots = [
            lot for lot in all_lots
            if lot.get("db") and lot.get("changed") and not lot.get("delete")
        ]
        new_lots = [lot for lot in final_lots if not lot.get("db")]

        try:
            for lot in partial_lots:
                _daily_update_partial_lot(lot)

            _daily_delete_lots(delete_lots)
            _daily_normalize_existing_products(final_lots)
            inserted_new = _daily_insert_lots(new_lots)

            # Final safety verification: active holdings must never be negative.
            final_db = supabase.table("holdings").select(
                "id,quantity,buy_price,client_id,symbol"
            ).execute().data or []
            negative = [
                r for r in final_db
                if _clean_number(r.get("quantity")) < -1e-9
            ]
            if negative:
                raise RuntimeError("Negative holding detected after daily update.")

            last_trade_rows = display_rows[:1000]
            last_upload_info = {
                "file_name": filename,
                "sheet_name": sheet_name,
                "records_read": len(raw_rows),
                "holdings_inserted": len(inserted_new),
                "old_holdings_removed": len(delete_lots),
                "status": "Processed - legacy V3 daily trade logic applied",
                "upload_date": datetime.now().strftime("%d-%m-%Y %H:%M:%S"),
                "logic": "BUY first; same-price buys aggregated; different prices separate; SELL lowest BUY price first; excess SELL ignored; no negative holdings",
                "summary": stats,
            }
            return jsonify({
                "success": True,
                "message": (
                    f"Daily trade update completed using legacy V3 logic. "
                    f"BUY lots created: {stats['lots_created']}; "
                    f"full lots closed: {stats['fully_closed']}; "
                    f"partial lots changed: {stats['partial_closes']}; "
                    f"excess SELL ignored: {stats['sell_excess_ignored']}."
                ),
                "records_read": len(raw_rows),
                "new_lots_created": stats["lots_created"],
                "full_lots_closed": stats["fully_closed"],
                "partial_lots_changed": stats["partial_closes"],
                "excess_sell_ignored": stats["sell_excess_ignored"],
                "sell_no_buy_ignored": stats["sell_ignored_no_buy"],
                "redirect": url_for("trade_report"),
            })

        except Exception as mutation_error:
            _daily_restore_snapshot(old_rows, old_product_map)
            raise mutation_error

    except Exception as exc:
        print("ERROR: /api/trade-report/upload:", repr(exc))
        return jsonify({
            "success": False,
            "message": "Trade report processing failed.",
            "error": str(exc),
            "code": "TRADE_REPORT_PROCESSING_ERROR",
        }), 500


# -------------------------
# Downloads
# -------------------------

def _csv_response(rows, columns, filename):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(columns)
    for row in rows:
        writer.writerow([row.get(c, "") for c in columns])
    return Response(output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.route("/download/holdings.csv")
def download_holdings():
    account, auth_error = _require_supervisor()
    if auth_error:
        return auth_error
    db_error = _require_db()
    if db_error:
        return db_error
    try:
        rows = _filter_supervisor_holdings(session.get("supervisor_id"), _load_all_holdings())
        export = [{"Client ID": r["client_id"], "Symbol": r["symbol"], "Exchange": r["product"],
                   "MTM": r["mtm"], "LTP": r["ltp"], "Qty": r["qty"], "Buy Price": r["buy_price"],
                   "Change": r["ltp"] - r["buy_price"], "Portfolio ID": r.get("id") or r.get("portfolio_id") or ""}
                  for r in rows]
        return _csv_response(export, list(export[0].keys()) if export else ["Client ID", "Symbol", "Exchange", "MTM", "LTP", "Qty", "Buy Price", "Change", "Portfolio ID"], "lms_portfolio.csv")
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route("/download/client-portfolio.csv")
def download_client_portfolio():
    db_error = _require_db()
    if db_error:
        return db_error
    cid = session.get("client_id")
    if not cid:
        return redirect(url_for("login"))
    try:
        result = supabase.table("holdings").select("*").ilike("client_id", cid).execute()
        rows = []
        for h in result.data or []:
            qty = _clean_number(h.get("quantity")); buy = _clean_number(h.get("buy_price")); ltp = _clean_number(h.get("ltp"), buy)
            rows.append({"Client ID": h.get("client_id"), "Symbol": h.get("symbol"), "Exchange": h.get("exchange"),
                         "MTM": (ltp-buy)*qty, "LTP": ltp, "Qty": qty, "Buy Price": buy, "Change": ltp-buy,
                         "Portfolio ID": h.get("id") or h.get("portfolio_id") or ""})
        rows.sort(key=lambda x: x["MTM"], reverse=True)
        cols = ["Client ID", "Symbol", "Exchange", "MTM", "LTP", "Qty", "Buy Price", "Change", "Portfolio ID"]
        return _csv_response(rows, cols, f"portfolio_{cid}.csv")
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route("/download/clients.csv")
def download_clients():
    account, auth_error = _require_supervisor()
    if auth_error:
        return auth_error
    db_error = _require_db()
    if db_error:
        return db_error
    try:
        clients = _filter_supervisor_clients(session.get("supervisor_id"), _load_all_clients())
        rows = [{"Client ID": c.get("client_id"), "Mobile": c.get("mobile"), "Email": c.get("email"),
                 "DOB": c.get("dob"), "Status": c.get("status")} for c in clients]
        return _csv_response(rows, ["Client ID", "Mobile", "Email", "DOB", "Status"], "lms_clients.csv")
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


# -------------------------
# Holdings add / modify / delete
# -------------------------

def _canonical_client(client_id):
    result = supabase.table("clients").select("client_id").ilike("client_id", str(client_id).strip()).limit(1).execute()
    return result.data[0]["client_id"] if result.data else None


def _holding_client_for_key(key):
    for col in ("id", "portfolio_id"):
        try:
            q = supabase.table("holdings").select("client_id").eq(col, key).limit(1).execute()
            if q.data:
                return q.data[0].get("client_id")
        except Exception:
            pass
    return None


def _holding_update(key, payload):
    data = {"symbol": str(payload.get("symbol") or "").strip().upper(),
            "exchange": str(payload.get("exchange") or "BSE").strip().upper(),
            "quantity": _clean_number(payload.get("quantity")),
            "buy_price": _clean_number(payload.get("buy_price"))}
    # Preserve existing LTP on modify; market data remains read-only.
    current = None
    for col in ("id", "portfolio_id"):
        try:
            q = supabase.table("holdings").select("ltp,client_id").eq(col, key).limit(1).execute()
            if q.data:
                current = q.data[0]; break
        except Exception:
            pass
    if current:
        data["client_id"] = current.get("client_id")
        data["ltp"] = _clean_number(current.get("ltp"), data["buy_price"])
    else:
        data["client_id"] = _canonical_client(payload.get("client_id"))
        data["ltp"] = data["buy_price"]
    data["market_value"] = data["quantity"] * data["ltp"]
    data["pnl"] = (data["ltp"] - data["buy_price"]) * data["quantity"]
    last_error = None
    for col in ("id", "portfolio_id"):
        try:
            result = supabase.table("holdings").update(data).eq(col, key).execute()
            if result.data:
                if "product" in payload:
                    product = str(payload.get("product") or "").strip().upper() or "-"
                    pm = _load_product_map()
                    sid = str(result.data[0].get("id") or key).strip()
                    pid = str(result.data[0].get("portfolio_id") or "").strip()
                    if sid: pm[sid] = product
                    if pid: pm["P:" + pid] = product
                    _save_product_map(pm)
                return result.data
        except Exception as exc:
            last_error = exc
    if last_error:
        raise last_error
    return []


@app.route("/api/holdings/add", methods=["POST"])
def add_holding():
    data = request.get_json(silent=True) or {}
    requested_client = data.get("client_id")
    actor_type, account, auth_error = _require_owner_or_supervisor(requested_client, can_manage=True)
    if auth_error:
        return auth_error
    db_error = _require_db()
    if db_error: return db_error
    client_id = _canonical_client(data.get("client_id"))
    if not client_id:
        return jsonify({"success": False, "message": "Client ID not found."}), 400
    if actor_type == "supervisor" and not _supervisor_can_view(session.get("supervisor_id"), client_id):
        return jsonify({"success": False, "message": "Client is outside this supervisor's access scope."}), 403
    if actor_type == "client" and str(client_id).lower() != str(session.get("client_id") or "").lower():
        return jsonify({"success": False, "message": "You can only add positions to your own portfolio."}), 403
    symbol = str(data.get("symbol") or "").strip().upper()
    qty = _clean_number(data.get("quantity")); buy = _clean_number(data.get("buy_price"))
    exchange = str(data.get("exchange") or "BSE").strip().upper()
    product = str(data.get("product") or exchange).strip().upper()
    if not client_id or not symbol or qty == 0 or buy == 0:
        return jsonify({"success": False, "message": "Valid Client ID, Symbol, Qty and Buy Price are required."}), 400
    try:
        row = {"client_id": client_id, "symbol": symbol, "exchange": exchange, "quantity": qty,
               "buy_price": buy, "ltp": buy, "market_value": qty * buy, "pnl": 0}
        result = supabase.table("holdings").insert(row).execute()
        saved_rows = result.data or []
        if saved_rows:
            product_map = _load_product_map()
            saved = saved_rows[0]
            sid = str(saved.get("id") or "").strip()
            pid = str(saved.get("portfolio_id") or "").strip()
            if sid:
                product_map[sid] = product
            if pid:
                product_map["P:" + pid] = product
            _save_product_map(product_map)
        return jsonify({"success": True, "data": saved_rows})
    except Exception as exc:
        return jsonify({"success": False, "message": "Add failed.", "error": str(exc)}), 500


@app.route("/api/holdings/modify", methods=["POST"])
def modify_holding():
    data = request.get_json(silent=True) or {}
    key = data.get("id") or data.get("portfolio_id")
    if key in (None, ""):
        return jsonify({"success": False, "message": "Portfolio ID is required."}), 400
    target_client = _holding_client_for_key(key)
    actor_type, account, auth_error = _require_owner_or_supervisor(target_client, can_manage=True)
    if auth_error:
        return auth_error
    if actor_type == "supervisor" and not _supervisor_can_view(session.get("supervisor_id"), target_client):
        return jsonify({"success": False, "message": "Portfolio record is outside this supervisor's access scope."}), 403
    try:
        updated = _holding_update(key, data)
        if not updated:
            return jsonify({"success": False, "message": "Portfolio record not found."}), 404
        return jsonify({"success": True, "data": updated})
    except Exception as exc:
        return jsonify({"success": False, "message": "Modify failed.", "error": str(exc)}), 500


@app.route("/api/holdings/delete", methods=["POST"])
def delete_holding():
    data = request.get_json(silent=True) or {}
    key = data.get("id") or data.get("portfolio_id")
    if key in (None, ""):
        return jsonify({"success": False, "message": "Portfolio ID is required."}), 400
    target_client = _holding_client_for_key(key)
    actor_type, account, auth_error = _require_owner_or_supervisor(target_client, can_manage=True)
    if auth_error:
        return auth_error
    if actor_type == "supervisor" and not _supervisor_can_view(session.get("supervisor_id"), target_client):
        return jsonify({"success": False, "message": "Portfolio record is outside this supervisor's access scope."}), 403
    try:
        for col in ("id", "portfolio_id"):
            try:
                result = supabase.table("holdings").delete().eq(col, key).execute()
                if result.data:
                    return jsonify({"success": True, "data": result.data})
            except Exception:
                pass
        return jsonify({"success": False, "message": "Portfolio record not found."}), 404
    except Exception as exc:
        return jsonify({"success": False, "message": "Delete failed.", "error": str(exc)}), 500


@app.route("/api/holdings/delete-selected", methods=["POST"])
def delete_selected_holdings():
    """Bulk delete selected portfolio rows; admin/supervisor dashboard only."""
    account, auth_error = _require_supervisor(can_manage=True)
    if auth_error:
        return auth_error
    db_error = _require_db()
    if db_error:
        return db_error
    data = request.get_json(silent=True) or {}
    ids = data.get("ids") or []
    if not isinstance(ids, list) or not ids:
        return jsonify({"success": False, "message": "Select at least one portfolio row."}), 400

    # Normalize and deduplicate IDs while preserving order.
    clean_ids = []
    seen = set()
    for value in ids:
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        clean_ids.append(text)
    if not clean_ids:
        return jsonify({"success": False, "message": "No valid portfolio IDs were selected."}), 400

    try:
        # Resolve selected records first so scope is checked before any deletion.
        selected_rows = []
        for key in clean_ids:
            found = None
            for col in ("id", "portfolio_id"):
                try:
                    result = supabase.table("holdings").select("id,portfolio_id,client_id").eq(col, key).limit(1).execute()
                    if result.data:
                        found = result.data[0]
                        break
                except Exception:
                    pass
            if found:
                selected_rows.append(found)

        if len(selected_rows) != len(clean_ids):
            return jsonify({"success": False, "message": "One or more selected portfolio records were not found."}), 404

        for row in selected_rows:
            cid = row.get("client_id")
            if not _supervisor_can_view(session.get("supervisor_id"), cid):
                return jsonify({"success": False, "message": "One or more selected records are outside your access scope."}), 403

        deleted = 0
        # Use the primary `id` for deletion because every rendered row carries it.
        for row in selected_rows:
            result = supabase.table("holdings").delete().eq("id", row.get("id")).execute()
            if result.data:
                deleted += len(result.data)

        return jsonify({"success": True, "message": f"Deleted {deleted} selected portfolio record(s).", "deleted": deleted})
    except Exception as exc:
        return jsonify({"success": False, "message": "Bulk delete failed.", "error": str(exc)}), 500


@app.route("/api/clients/reset-password", methods=["POST"])
def reset_passwords():
    account, auth_error = _require_supervisor(can_manage=True)
    if auth_error:
        return auth_error
    db_error = _require_db()
    if db_error: return db_error
    data = request.get_json(silent=True) or {}
    ids = data.get("client_ids") or []
    if not isinstance(ids, list) or not ids:
        return jsonify({"success": False, "message": "Select at least one client."}), 400
    try:
        all_clients = _load_all_clients()
        cmap = {str(c.get("client_id") or "").lower(): c.get("client_id") for c in all_clients}
        changed = 0
        for cid in ids:
            canonical = cmap.get(str(cid).strip().lower())
            if canonical and _supervisor_can_view(session.get("supervisor_id"), canonical):
                supabase.table("clients").update({"password_hash": canonical, "status": "active"}).eq("client_id", canonical).execute()
                changed += 1
        return jsonify({"success": True, "message": f"Password reset to Client ID for {changed} client(s)."})
    except Exception as exc:
        return jsonify({"success": False, "message": "Password reset failed.", "error": str(exc)}), 500


@app.route("/api/login", methods=["POST"])
def api_login():
    if supabase is None:
        return jsonify({"success": False, "message": supabase_config_error}), 500
    data = request.get_json(silent=True) or {}
    client_id = str(data.get("client_id") or "").strip(); password = str(data.get("password") or "").strip()
    if not client_id or not password:
        return jsonify({"success": False, "message": "Client ID and Password are required."}), 400
    try:
        result = supabase.table("clients").select("client_id,password_hash,status").ilike("client_id", client_id).limit(1).execute()
        if not result.data: return jsonify({"success": False, "message": "Invalid Client ID or Password."}), 401
        client = result.data[0]
        if str(client.get("status") or "").lower() not in ("", "active"):
            return jsonify({"success": False, "message": "This account is not active."}), 403
        if str(client.get("password_hash") or "").lower() != password.lower():
            return jsonify({"success": False, "message": "Invalid Client ID or Password."}), 401
        return jsonify({"success": True, "client_id": client.get("client_id")})
    except Exception as exc:
        return jsonify({"success": False, "message": "Login error", "error": str(exc)}), 500


@app.route("/api/portfolio/<client_id>")
def get_portfolio(client_id):
    if supabase is None: return jsonify({"success": False, "message": supabase_config_error}), 500
    try:
        result = supabase.table("holdings").select("*").ilike("client_id", client_id).execute()
        return jsonify({"success": True, "data": result.data or []})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
