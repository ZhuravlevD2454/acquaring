Первый запуск

py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe seed.py
.\.venv\Scripts\python.exe app.py


Раздел	Адрес
Главная	http://127.0.0.1:8000/
Вход в ЛК	http://127.0.0.1:8000/login
ИМ Ромашка	http://127.0.0.1:8000/shop/romashka
ИМ Василек	http://127.0.0.1:8000/shop/vasilek
Отдельный сайт заказов	http://127.0.0.1:8001/orders