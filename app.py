import hmac
import json
import re
import secrets
import sqlite3
from decimal import Decimal, InvalidOperation
from functools import wraps
from threading import Thread

from flask import (
    Flask, abort, flash, g, redirect, render_template, request, session, url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.serving import make_server

from storage import DATA, connect, write

MAIN = "http://127.0.0.1:8000"
ORDERS = "http://127.0.0.1:8001"
VAT = {"none": "Без НДС", "0": "0%", "5": "5%", "7": "7%", "10": "10%", "22": "22%"}
UNITS = {0: "шт.", 11: "кг", 41: "л"}
METHODS = {1: "Предоплата 100%", 4: "Полный расчет"}
KINDS = {"initial": "Приход", "settlement": "Приход — зачет аванса",
         "refund": "Возврат прихода"}

secret = DATA / "secret.key"
if not secret.is_file():
    raise SystemExit("Сначала выполните: python seed.py")

app = Flask(__name__)
app.config.update(
    SECRET_KEY=secret.read_text(encoding="ascii").strip(),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    MAX_CONTENT_LENGTH=64 * 1024,
    TRUSTED_HOSTS=["127.0.0.1"],
)


class Problem(Exception):
    pass


@app.errorhandler(Problem)
def bad_input(error):
    return render_template("error.html", message=str(error)), 400


@app.errorhandler(403)
@app.errorhandler(404)
def inaccessible(error):
    text = "Нет доступа к этому действию." if error.code == 403 else "Страница не найдена."
    return render_template("error.html", message=text), error.code


def one(sql, args=()):
    return g.db.execute(sql, args).fetchone()


def rows(sql, args=()):
    return g.db.execute(sql, args).fetchall()


def add(sql, args=()):
    return g.db.execute(sql, args).lastrowid


def on_orders_port():
    return request.environ.get("SERVER_PORT") == "8001"


@app.before_request
def prepare():
    if on_orders_port() and request.endpoint not in {"orders_page", "order_action", "static"}:
        abort(404)
    g.db = connect()
    session.setdefault("csrf", secrets.token_hex(24))
    session.setdefault("browser", secrets.token_hex(24))
    g.user = one("SELECT * FROM sellers WHERE id = ?", (session.get("seller_id"),))
    if request.method == "POST":
        if not hmac.compare_digest(
            request.form.get("csrf", "").encode(), session["csrf"].encode()
        ):
            abort(403)


@app.teardown_appcontext
def close_db(_error):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@app.template_filter("money")
def money(value):
    return f"{value // 100:,}".replace(",", " ") + f",{value % 100:02d} ₽"


@app.context_processor
def common():
    return dict(
        main_url=MAIN, orders_url=ORDERS, vat_labels=VAT, units=UNITS,
        methods=METHODS, kinds=KINDS, csrf=session.get("csrf"),
    )


def protected(admin=False):
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            if not g.user:
                flash("Войдите в личный кабинет.", "error")
                return redirect(MAIN + url_for("login"))
            if admin and g.user["role"] != "admin":
                abort(403)
            return fn(*args, **kwargs)
        return wrapped
    return decorate


def owns(site):
    return g.user is not None and (
        g.user["role"] == "admin" or site["seller_id"] == g.user["id"]
    )


def get_site(slug, public=False):
    s = one(
        """SELECT s.*, u.login, u.legal_name, u.tax_id
           FROM sites s JOIN sellers u ON u.id=s.seller_id WHERE s.slug=?""",
        (slug,),
    )
    if s is None or (public and not s["active"]):
        abort(404)
    return s


def payment_for_user(pid):
    p = one(
        """SELECT p.*, s.seller_id, s.slug, s.name AS site_name, s.kind, u.login
           FROM payments p JOIN sites s ON s.id=p.site_id
           JOIN sellers u ON u.id=s.seller_id WHERE p.id=?""",
        (pid,),
    )
    if p is None or not owns(p):
        abort(404)
    return p


def scope():
    return ("1=1", ()) if g.user["role"] == "admin" else (
        "s.seller_id=?", (g.user["id"],)
    )


def number(raw, minimum, maximum, label):
    if not re.fullmatch(r"[0-9]{1,9}", raw or ""):
        raise Problem(f"{label}: введите целое число.")
    value = int(raw)
    if not minimum <= value <= maximum:
        raise Problem(f"{label}: допустимо от {minimum} до {maximum}.")
    return value


def price_from_form(raw):
    raw = raw.strip().replace(",", ".")
    if not re.fullmatch(r"[0-9]{1,7}(?:\.[0-9]{1,2})?", raw):
        raise Problem("Цена: положительная сумма, не более двух знаков после запятой.")
    try:
        cents = int(Decimal(raw) * 100)
    except InvalidOperation:
        raise Problem("Некорректная цена.") from None
    if cents <= 0:
        raise Problem("Цена должна быть больше нуля.")
    return cents


def choice(value, choices, label):
    if value not in choices:
        raise Problem(f"Недопустимое значение: {label}.")
    return value


def mark_value(raw, required=False):
    value = raw.strip()
    if not value and not required:
        return ""
    if not re.fullmatch(r"DEMO-[A-Za-z0-9-]{1,90}", value):
        raise Problem("Введите учебный код маркировки, например DEMO-0001.")
    return value


def unique_marks(lines):
    codes = [i["mark_code"] for i in lines if i["mark_code"]]
    if len(codes) != len(set(codes)):
        raise Problem("Один и тот же код маркировки нельзя использовать дважды в чеке.")


def kkt_value():
    return choice(request.form.get("kkt"), ("ok", "error"), "ответ ККТ")


def card_info(raw):
    pan = raw.strip()
    if not re.fullmatch(r"[0-9]{4}00000000[0-9]{4}", pan):
        raise Problem("Используйте только учебный номер формата 1111000000001111.")
    # Дополнительно отклоняем номера с корректной контрольной суммой Луна.
    digits = [int(x) for x in pan]
    doubled = [v * 2 if i % 2 == 0 else v for i, v in enumerate(digits)]
    if sum(v - 9 if v > 9 else v for v in doubled) % 10 == 0:
        raise Problem("Номер с корректной контрольной суммой не принимается стендом.")
    prefix = int(pan[:4])
    bank = "ХраниБанк" if prefix <= 3333 else "Ы-Банк" if prefix <= 6666 else "БКБ"
    return bank, pan[:4] + " **** **** " + pan[-4:]


def cart(site_id):
    saved = one(
        "SELECT items_json FROM carts WHERE browser_key=? AND site_id=?",
        (session["browser"], site_id),
    )
    return json.loads(saved["items_json"]) if saved else []


def save_cart(site_id, items):
    if not items:
        g.db.execute(
            "DELETE FROM carts WHERE browser_key=? AND site_id=?",
            (session["browser"], site_id),
        )
    else:
        g.db.execute(
            """INSERT INTO carts VALUES (?, ?, ?)
               ON CONFLICT(browser_key, site_id)
               DO UPDATE SET items_json=excluded.items_json""",
            (session["browser"], site_id, json.dumps(items, ensure_ascii=False)),
        )


def fiscal_context(s, email):
    return {
        "1008": email, "1018": s["tax_id"], "1048": s["legal_name"],
        "1009": MAIN + url_for("shop_page", slug=s["slug"]),
        "1187": s["name"], "1055": 1, "1125": 1,
    }


def make_receipt(pid, kind, items, status, context, rid=None):
    total = sum(i["qty"] * i["price"] for i in items)
    advance = total if kind == "settlement" else 0
    receipt_id = add(
        """INSERT INTO receipts
           (payment_id, refund_id, kind, total, cashless, advance,
            kkt_status, kkt_code, fiscal_context)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (pid, rid, kind, total, total - advance, advance, status,
         "KKT_OK" if status == "ok" else "KKT_UNAVAILABLE", context),
    )
    line_ids = []
    for i in items:
        amount = i["qty"] * i["price"]
        vat = None
        if i["vat"] != "none":
            rate = int(i["vat"])
            divisor = 100 + rate
            vat = (2 * amount * rate + divisor) // (2 * divisor)
        line_ids.append(add(
            """INSERT INTO receipt_items
               (receipt_id, source_item_id, name, qty, price, vat, unit,
                marked, mark_code, method, line_total, vat_amount)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (receipt_id, i.get("source_item_id"), i["name"], i["qty"],
             i["price"], i["vat"], i["unit"], i["marked"], i["mark_code"],
             i["method"], amount, vat),
        ))
    return receipt_id, line_ids


def receipt_view(r):
    items = rows("SELECT * FROM receipt_items WHERE receipt_id=? ORDER BY id", (r["id"],))
    # Читаемая модель запроса, а не сериализованный документ ККТ.
    payload = {
        "версия модели": "ФФД 1.2",
        "денежные поля": "копейки",
        "теги": {
            **json.loads(r["fiscal_context"]),
            "1012": r["created_at"] + " UTC",
            "1054": 2 if r["kind"] == "refund" else 1,
            "1020": r["total"], "1081": r["cashless"], "1215": r["advance"],
            "1059": [
                {
                    "1030": i["name"], "1023": i["qty"], "1079": i["price"],
                    "1043": i["line_total"], "2108": i["unit"], "1214": i["method"],
                    "1212": (33 if i["mark_code"] else 32) if i["marked"] else 1,
                    "1199": vat_label(i["vat"]),
                    **({"1200": i["vat_amount"]} if i["vat_amount"] is not None else {}),
                    **({"учебный код маркировки": i["mark_code"]} if i["mark_code"] else {}),
                }
                for i in items
            ],
        },
    }
    return {"row": r, "items": items, "payload": payload}


def vat_label(key):
    return VAT[key]


@app.get("/")
def home():
    return render_template("index.html", sites=rows("SELECT * FROM sites WHERE active=1 ORDER BY id"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = one("SELECT * FROM sellers WHERE login=?", (request.form.get("login", "").strip(),))
        if user and check_password_hash(user["password_hash"], request.form.get("password", "")):
            browser = session["browser"]
            session.clear()
            session.update(seller_id=user["id"], browser=browser, csrf=secrets.token_hex(24))
            return redirect(url_for("payments"), code=303)
        flash("Неверный логин или пароль.", "error")
    return render_template("login.html")


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"), code=303)


def order_cards(site_id):
    result = []
    for o in rows(
        """SELECT o.*, p.total FROM shop.orders o
           JOIN payments p ON p.id=o.payment_id
           WHERE p.site_id=? ORDER BY o.payment_id DESC""", (site_id,),
    ):
        items = rows(
            "SELECT * FROM shop.order_items WHERE payment_id=? ORDER BY source_item_id",
            (o["payment_id"],),
        )
        attempts = rows(
            """SELECT id, kind, kkt_status, kkt_code FROM receipts
               WHERE payment_id=? ORDER BY id""",
            (o["payment_id"],),
        )
        result.append(dict(
            row=o, items=items, attempts=attempts,
            can_close=o["method"] == 1 and o["closed_receipt_id"] is None
            and any(i["qty"] > i["refunded"] for i in items),
        ))
    return result


@app.get("/shop/<slug>")
def shop_page(slug):
    s = get_site(slug, public=True)
    items = cart(s["id"])
    return render_template(
        s["kind"] + ".html", site=s, items=items, view="shop",
        cards=order_cards(s["id"]) if s["kind"] == "vasilek" and owns(s) else [],
        can_manage=owns(s), total=sum(i["qty"] * i["price"] for i in items),
    )


@app.post("/shop/<slug>/cart")
def edit_cart(slug):
    s = get_site(slug, public=True)
    back = url_for("shop_page", slug=slug)
    try:
        with write(g.db):
            items = cart(s["id"])
            action = choice(request.form.get("action"), ("add", "clear"), "действие")
            if action == "clear":
                save_cart(s["id"], [])
            else:
                if len(items) >= 30:
                    raise Problem("Для одного тестового платежа допустимо до 30 позиций.")
                f = request.form
                name = f.get("name", "").strip()
                if not 1 <= len(name) <= 128 or not name.isprintable():
                    raise Problem("Наименование должно содержать от 1 до 128 символов.")
                method = 4
                if s["kind"] == "vasilek":
                    method = items[0]["method"] if items else number(
                        f.get("method"), 1, 4, "Способ расчета"
                    )
                    choice(method, METHODS, "способ расчета")
                marked = int(s["kind"] == "vasilek" and f.get("marked") == "1")
                qty = 1 if marked else number(f.get("qty"), 1, 9999, "Количество")
                unit = 0 if marked else number(f.get("unit"), 0, 99, "Единица измерения")
                code = mark_value(f.get("mark_code", ""), required=True) \
                    if marked and method == 4 else ""
                items.append(dict(
                    name=name, qty=qty, price=price_from_form(f.get("price", "")),
                    vat=choice(f.get("vat"), VAT, "НДС"),
                    unit=choice(unit, UNITS, "единица измерения"),
                    marked=marked, mark_code=code, method=method,
                ))
                unique_marks(items)
                save_cart(s["id"], items)
    except Problem as error:
        flash(str(error), "error")
    return redirect(back, code=303)


@app.post("/shop/<slug>/pay")
def pay(slug):
    s = get_site(slug, public=True)
    try:
        bank, masked = card_info(request.form.get("card", ""))
        bank_result = choice(request.form.get("bank"), ("ok", "decline"), "ответ банка")
        kkt_status = kkt_value()
        email = request.form.get("email", "").strip()
        if not re.fullmatch(r"[A-Za-z0-9._+-]{1,64}@[A-Za-z0-9.-]{1,70}\.test", email):
            raise Problem("Используйте учебный email с доменом .test, например buyer@example.test.")
        with write(g.db):
            items = cart(s["id"])
            if not items:
                raise Problem("Добавьте хотя бы одну позицию.")
            result = dict(bank=bank, masked=masked, pid=None, receipt=None, kkt=None)
            if bank_result == "ok":
                unique_marks(items)
                total = sum(i["qty"] * i["price"] for i in items)
                pid = add("INSERT INTO payments (site_id, total) VALUES (?, ?)", (s["id"], total))
                add(
                    """INSERT INTO bank_transactions (payment_id, bank, card_mask, result_code)
                       VALUES (?, ?, ?, 'BANK_APPROVED')""",
                    (pid, bank, masked),
                )
                context = json.dumps(fiscal_context(s, email), ensure_ascii=False)
                receipt_id, item_ids = make_receipt(pid, "initial", items, kkt_status, context)
                if s["kind"] == "vasilek":
                    g.db.execute(
                        """INSERT INTO shop.orders (payment_id, method, closed_receipt_id)
                           VALUES (?, ?, ?)""",
                        (pid, items[0]["method"],
                         receipt_id if items[0]["method"] == 4 and kkt_status == "ok" else None),
                    )
                    for source_id, i in zip(item_ids, items):
                        g.db.execute(
                            """INSERT INTO shop.order_items
                               (source_item_id, payment_id, name, qty, price,
                                vat, unit, marked, mark_code)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (source_id, pid, i["name"], i["qty"], i["price"],
                             i["vat"], i["unit"], i["marked"], i["mark_code"]),
                        )
                save_cart(s["id"], [])
                result.update(
                    pid=pid, receipt=receipt_id,
                    kkt="KKT_OK" if kkt_status == "ok" else "KKT_UNAVAILABLE",
                )
            result["bank_code"] = "BANK_APPROVED" if bank_result == "ok" else "BANK_DECLINED"
        session["outcome"] = {"slug": slug, **result}
        return redirect(url_for("pay_outcome", slug=slug,
                                status="success" if result["pid"] else "failure"), code=303)
    except Problem as error:
        flash(str(error), "error")
        return redirect(url_for("shop_page", slug=slug), code=303)


@app.get("/shop/<slug>/<any(success, failure):status>")
def pay_outcome(slug, status):
    s = get_site(slug, public=True)
    result = session.get("outcome")
    if not result or result["slug"] != slug or bool(result["pid"]) != (status == "success"):
        return redirect(url_for("shop_page", slug=slug))
    return render_template(f"{s['kind']}_{status}.html", site=s, result=result)


@app.get("/lk/payments")
@protected()
def payments():
    where, args = scope()
    return render_template("payments.html", payments=rows(
        f"""SELECT p.*, s.name AS site_name, u.login, t.bank, t.card_mask,
                   t.result_code, r.kkt_code
            FROM payments p JOIN sites s ON s.id=p.site_id
            JOIN sellers u ON u.id=s.seller_id
            JOIN bank_transactions t ON t.payment_id=p.id
            JOIN receipts r ON r.payment_id=p.id AND r.kind='initial'
            WHERE {where} ORDER BY p.id DESC""", args,
    ))


@app.get("/lk/payments/<int:pid>")
@protected()
def payment_detail(pid):
    p = payment_for_user(pid)
    initial_and_full = [
        receipt_view(r) for r in rows(
            "SELECT * FROM receipts WHERE payment_id=? AND refund_id IS NULL ORDER BY id",
            (pid,),
        )
    ]
    refund_cards = [
        dict(row=r, receipt=receipt_view(one("SELECT * FROM receipts WHERE refund_id=?", (r["id"],))))
        for r in rows("SELECT * FROM refunds WHERE payment_id=? ORDER BY id", (pid,))
    ]
    return render_template(
        "payment.html", payment=p, receipts=initial_and_full, refunds=refund_cards,
        tx=one("SELECT * FROM bank_transactions WHERE payment_id=?", (pid,)),
    )


def refundable(pid):
    return rows(
        """SELECT i.*, i.qty - COALESCE((
               SELECT SUM(ri.qty) FROM receipt_items ri
               JOIN receipts rr ON rr.id=ri.receipt_id
               WHERE ri.source_item_id=i.id AND rr.kind='refund'
           ), 0) AS available
           FROM receipt_items i JOIN receipts r ON r.id=i.receipt_id
           WHERE r.payment_id=? AND r.kind='initial' ORDER BY i.id""",
        (pid,),
    )


@app.route("/lk/payments/<int:pid>/refund", methods=["GET", "POST"])
@protected()
def refund(pid):
    p = payment_for_user(pid)
    if request.method == "GET":
        return render_template("refund.html", payment=p, items=refundable(pid),
                               request_key=secrets.token_hex(16))
    back = url_for("payment_detail", pid=pid)
    try:
        key = request.form.get("request_key", "")
        if not re.fullmatch(r"[a-f0-9]{32}", key):
            raise Problem("Откройте форму возврата заново.")
        with write(g.db):
            if one("SELECT 1 FROM refunds WHERE request_key=?", (key,)):
                flash("Этот запрос возврата уже обработан.", "info")
                return redirect(back, code=303)
            initial = one(
                "SELECT * FROM receipts WHERE payment_id=? AND kind='initial'", (pid,)
            )
            settlement = one(
                """SELECT id FROM receipts WHERE payment_id=?
                   AND kind='settlement' AND kkt_status='ok'""", (pid,)
            )
            items = []
            for line in refundable(pid):
                qty = number(request.form.get(f"q_{line['id']}", "0"),
                             0, line["available"], f"Количество для «{line['name']}»")
                if not qty:
                    continue
                i = dict(line)
                i.update(qty=qty, source_item_id=line["id"])
                if settlement:
                    settled = one(
                        "SELECT * FROM receipt_items WHERE receipt_id=? AND source_item_id=?",
                        (settlement["id"], line["id"]),
                    )
                    if settled is None:
                        raise Problem("Позиция не входит в завершенный расчет.")
                    i.update(method=4, mark_code=settled["mark_code"])
                items.append(i)
            if not items:
                raise Problem("Укажите количество хотя бы для одной позиции.")
            amount = sum(i["qty"] * i["price"] for i in items)
            rid = add(
                """INSERT INTO refunds (payment_id, amount, bank_code, request_key)
                   VALUES (?, ?, 'BANK_REFUND_APPROVED', ?)""",
                (pid, amount, key),
            )
            make_receipt(pid, "refund", items, kkt_value(), initial["fiscal_context"], rid)
            if p["kind"] == "vasilek":
                for i in items:
                    g.db.execute(
                        "UPDATE shop.order_items SET refunded=refunded+? WHERE source_item_id=?",
                        (i["qty"], i["source_item_id"]),
                    )
        flash(f"Возврат №{rid} проведен. Ответ ККТ — в карточке платежа.", "info")
        return redirect(back, code=303)
    except Problem as error:
        flash(str(error), "error")
        return redirect(url_for("refund", pid=pid), code=303)


@app.get("/lk/refunds")
@protected()
def refunds():
    where, args = scope()
    return render_template("refunds.html", refunds=rows(
        f"""SELECT f.*, s.name AS site_name, u.login, r.kkt_code
            FROM refunds f JOIN payments p ON p.id=f.payment_id
            JOIN sites s ON s.id=p.site_id JOIN sellers u ON u.id=s.seller_id
            JOIN receipts r ON r.refund_id=f.id
            WHERE {where} ORDER BY f.id DESC""", args,
    ))


@app.route("/lk/sites", methods=["GET", "POST"])
@protected()
def sites():
    if request.method == "POST":
        s = get_site(request.form.get("slug", ""))
        if not owns(s):
            abort(404)
        name = request.form.get("name", "").strip()
        if not 1 <= len(name) <= 100:
            raise Problem("Название сайта должно содержать от 1 до 100 символов.")
        with write(g.db):
            g.db.execute("UPDATE sites SET name=?, active=? WHERE id=?",
                         (name, int(request.form.get("active") == "1"), s["id"]))
        flash("Настройки сайта сохранены.", "info")
        return redirect(url_for("sites"), code=303)
    where, args = scope()
    return render_template("sites.html", sites=rows(
        f"SELECT s.*, u.login FROM sites s JOIN sellers u ON u.id=s.seller_id "
        f"WHERE {where} ORDER BY u.login, s.id", args,
    ))


@app.get("/lk/reports")
@protected()
def reports():
    where, args = scope()
    data = rows(
        f"""WITH pa AS (
                SELECT site_id, COUNT(*) AS n, SUM(total) AS gross
                FROM payments GROUP BY site_id
            ), re AS (
                SELECT p.site_id, COUNT(*) AS n, SUM(f.amount) AS returned
                FROM refunds f JOIN payments p ON p.id=f.payment_id GROUP BY p.site_id
            ), ka AS (
                SELECT p.site_id, COUNT(*) AS n FROM receipts r
                JOIN payments p ON p.id=r.payment_id WHERE r.kkt_status='error'
                GROUP BY p.site_id
            )
            SELECT s.name, u.login, COALESCE(pa.n,0) AS payments,
                   COALESCE(re.n,0) AS refunds, COALESCE(pa.gross,0) AS gross,
                   COALESCE(re.returned,0) AS returned, COALESCE(ka.n,0) AS errors
            FROM sites s JOIN sellers u ON u.id=s.seller_id
            LEFT JOIN pa ON pa.site_id=s.id LEFT JOIN re ON re.site_id=s.id
            LEFT JOIN ka ON ka.site_id=s.id WHERE {where} ORDER BY u.login, s.id""", args,
    )
    return render_template("reports.html", data=data)


@app.route("/lk/admin", methods=["GET", "POST"])
@protected(admin=True)
def admin():
    if request.method == "POST":
        f = request.form
        try:
            with write(g.db):
                if f.get("action") == "seller":
                    username = f.get("login", "").strip()
                    password = f.get("password", "")
                    name = f.get("legal_name", "").strip()
                    tax_id = f.get("tax_id", "").strip()
                    if not re.fullmatch(r"[A-Za-z0-9_-]{3,32}", username):
                        raise Problem("Логин: 3–32 латинские буквы, цифры, дефисы или подчеркивания.")
                    if not 8 <= len(password) <= 128 or not 1 <= len(name) <= 100:
                        raise Problem("Пароль: 8–128 символов. Название: 1–100 символов.")
                    if not re.fullmatch(r"(?:[0-9]{10}|[0-9]{12})", tax_id):
                        raise Problem("Учебный ИНН должен содержать 10 или 12 цифр.")
                    g.db.execute(
                        """INSERT INTO sellers (login, password_hash, role, legal_name, tax_id)
                           VALUES (?, ?, 'seller', ?, ?)""",
                        (username, generate_password_hash(password), name, tax_id),
                    )
                elif f.get("action") == "site":
                    sid = number(f.get("seller_id"), 1, 999999999, "Продавец")
                    if not one("SELECT 1 FROM sellers WHERE id=? AND role='seller'", (sid,)):
                        raise Problem("Выберите существующего продавца.")
                    slug = f.get("slug", "").strip()
                    name = f.get("name", "").strip()
                    if not re.fullmatch(r"[a-z0-9-]{2,40}", slug) or not 1 <= len(name) <= 100:
                        raise Problem("Укажите название и URL-имя из 2–40 строчных латинских букв, цифр или дефисов.")
                    kind = choice(f.get("kind"), ("romashka", "vasilek"), "тип магазина")
                    g.db.execute(
                        "INSERT INTO sites (seller_id, slug, name, kind) VALUES (?, ?, ?, ?)",
                        (sid, slug, name, kind),
                    )
                else:
                    raise Problem("Неизвестное действие.")
            flash("Запись добавлена.", "info")
        except sqlite3.IntegrityError:
            flash("Такой логин или URL-имя уже существует.", "error")
        except Problem as error:
            flash(str(error), "error")
        return redirect(url_for("admin"), code=303)
    return render_template("admin.html", sellers=rows(
        "SELECT id, login, legal_name FROM sellers WHERE role='seller' ORDER BY login"
    ))


@app.get("/orders")
@protected()
def orders_page():
    where, args = scope()
    eligible = rows(
        f"SELECT s.* FROM sites s WHERE s.kind='vasilek' AND {where} ORDER BY s.id", args
    )
    return render_template(
        "orders.html", view="orders",
        groups=[(s, order_cards(s["id"])) for s in eligible],
    )


@app.post("/orders/<slug>/<int:pid>")
@protected()
def order_action(slug, pid):
    s = get_site(slug)
    if s["kind"] != "vasilek" or not owns(s):
        abort(404)
    back = url_for("orders_page") if (
        on_orders_port() or request.form.get("view") != "shop" or not s["active"]
    ) else url_for("shop_page", slug=slug)
    try:
        action = choice(request.form.get("action"), ("save", "settle"), "действие")
        with write(g.db):
            o = one(
                """SELECT o.* FROM shop.orders o JOIN payments p ON p.id=o.payment_id
                   WHERE o.payment_id=? AND p.site_id=?""", (pid, s["id"]),
            )
            if o is None:
                abort(404)
            if o["method"] != 1 or o["closed_receipt_id"] is not None:
                raise Problem("Для этого заказа дополнительный полный расчет недоступен.")
            remaining = []
            for line in rows(
                "SELECT * FROM shop.order_items WHERE payment_id=? ORDER BY source_item_id", (pid,),
            ):
                if line["qty"] == line["refunded"]:
                    continue
                i = dict(line)
                i.update(qty=line["qty"] - line["refunded"], method=4)
                if i["marked"]:
                    i["mark_code"] = mark_value(
                        request.form.get(f"mark_{i['source_item_id']}", i["mark_code"])
                    )
                    if action == "settle" and not i["mark_code"]:
                        raise Problem(f"Не указан код маркировки для «{i['name']}».")
                    g.db.execute(
                        "UPDATE shop.order_items SET mark_code=? WHERE source_item_id=?",
                        (i["mark_code"], i["source_item_id"]),
                    )
                remaining.append(i)
            if not remaining:
                raise Problem("В заказе нет позиций, оставшихся к расчету.")
            unique_marks(remaining)
            if action == "settle":
                initial = one(
                    "SELECT fiscal_context FROM receipts WHERE payment_id=? AND kind='initial'",
                    (pid,),
                )
                status = kkt_value()
                receipt_id, _ = make_receipt(
                    pid, "settlement", remaining, status, initial["fiscal_context"]
                )
                if status == "ok":
                    g.db.execute(
                        "UPDATE shop.orders SET closed_receipt_id=? WHERE payment_id=?",
                        (receipt_id, pid),
                    )
                flash(f"Чек №{receipt_id}: " +
                      ("KKT_OK." if status == "ok" else "KKT_UNAVAILABLE. Можно повторить."), "info")
            else:
                flash("Коды маркировки сохранены.", "info")
    except Problem as error:
        flash(str(error), "error")
    return redirect(back, code=303)


if __name__ == "__main__":
    order_server = make_server("127.0.0.1", 8001, app, threaded=True)
    main_server = make_server("127.0.0.1", 8000, app, threaded=True)
    worker = Thread(target=order_server.serve_forever, daemon=True)
    worker.start()
    print(f"Эквайринг и магазины: {MAIN}", flush=True)
    print(f"Отдельный сайт заказов: {ORDERS}/orders", flush=True)
    try:
        main_server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        order_server.shutdown()
        order_server.server_close()
        main_server.server_close()