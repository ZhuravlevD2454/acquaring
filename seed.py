import secrets
from contextlib import closing

from werkzeug.security import generate_password_hash

from storage import DATA, ROOT, connect, write


def main():
    DATA.mkdir(exist_ok=True)
    secret = DATA / "secret.key"
    if not secret.exists():
        secret.write_text(secrets.token_hex(32), encoding="ascii")

    with closing(connect()) as db:
        db.execute("PRAGMA main.journal_mode = DELETE")
        db.execute("PRAGMA shop.journal_mode = DELETE")
        db.executescript((ROOT / "schema.sql").read_text(encoding="utf-8"))

        with write(db):
            for login, password, role, name, tax_id in (
                ("Admin", "1234", "admin", "Администратор стенда", "0000000000"),
                ("Romashka", "123", "seller", "ИМ Ромашка", "0000000001"),
                ("Vasilek", "123", "seller", "ИМ Василек", "0000000002"),
            ):
                if not db.execute(
                    "SELECT 1 FROM sellers WHERE login = ?", (login,)
                ).fetchone():
                    db.execute(
                        """INSERT INTO sellers
                           (login, password_hash, role, legal_name, tax_id)
                           VALUES (?, ?, ?, ?, ?)""",
                        (login, generate_password_hash(password), role, name, tax_id),
                    )

            for login, slug, name in (
                ("Romashka", "romashka", "ИМ Ромашка"),
                ("Vasilek", "vasilek", "ИМ Василек"),
            ):
                db.execute(
                    """INSERT OR IGNORE INTO sites (seller_id, slug, name, kind)
                       SELECT id, ?, ?, ? FROM sellers WHERE login = ?""",
                    (slug, name, slug, login),
                )

    print("Базы готовы. Существующие платежи, пароли и настройки не сброшены.")


if __name__ == "__main__":
    main()