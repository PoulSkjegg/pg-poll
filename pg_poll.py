#!/usr/bin/env python3
"""
pg_poll.py - runs a SQL file through psql every N seconds and overwrites
an output file with the result (a single number).

Standard library only, works on Python 3.7+. It lives as long as your
terminal session does; hit Ctrl+C to stop it.

Connection details are asked for at startup, the password without echo.
The password is never written anywhere: it stays in this process's memory
and is handed to each psql call through that child's environment only.
You won't find it in `ps` output or in any file.

The SQL file is re-read on every run, so you can tweak the query
without restarting the script.

Example:
  python3 /opt/tools/pg_poll.py -s /opt/tools/query.sql -o /data/out/result.txt -i 5m
"""
import argparse
import getpass
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime

# Accepts things like 42, -3, 0.5, 1e6 - whatever a numeric column can print.
NUM_RE = re.compile(r'^[-+]?\d+(\.\d+)?([eE][-+]?\d+)?$')


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("{} {}".format(ts, msg), file=sys.stderr, flush=True)


def parse_interval(text):
    """Turn '30', '30s', '5m' or '1h' into seconds."""
    m = re.fullmatch(r'\s*(\d+)\s*([smhSMH]?)\s*', text)
    if not m:
        raise argparse.ArgumentTypeError("интервал: число секунд или 30s / 5m / 1h")
    seconds = int(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2).lower()]
    if seconds < 1:
        raise argparse.ArgumentTypeError("интервал должен быть >= 1 секунды")
    return seconds


def ask(prompt, default=None, check=None):
    """Prompt until we get a usable answer. Plain Enter takes the default."""
    hint = " [{}]".format(default) if default else ""
    while True:
        value = input("{}{}: ".format(prompt, hint)).strip() or default
        if not value:
            print("  значение обязательно", file=sys.stderr)
        elif check and not check(value):
            print("  некорректное значение", file=sys.stderr)
        else:
            return value


class Poller:
    """Holds the connection settings and runs the query on demand.

    The psql command line and its environment are built once up front,
    so each run just spawns the process and reads what it printed.
    """

    def __init__(self, host, port, dbname, user, password, sql_path, timeout):
        self.timeout = timeout
        self.cmd = [
            "psql",
            "-X",                     # ignore ~/.psqlrc, it could change the output format
            "-w",                     # never prompt for a password - fail instead of hanging
            "-q",                     # no chatter like "SET" in the output
            "-A", "-t",               # bare value: no alignment, no header, no row count
            "-v", "ON_ERROR_STOP=1",  # make SQL errors show up as a non-zero exit code
            "-h", host, "-p", port, "-U", user, "-d", dbname,
            "-f", sql_path,
        ]
        self.env = dict(os.environ)
        self.env["PGPASSWORD"] = password      # visible to the child psql only
        self.env["PGPASSFILE"] = os.devnull    # don't let a stray ~/.pgpass sneak in
        self.env.setdefault("PGCONNECT_TIMEOUT", "10")
        self.env.setdefault("PGAPPNAME", "pg_poll")  # so the DBA can spot us in pg_stat_activity

    def query(self):
        """Run the SQL once and return the number as a string.

        Raises RuntimeError if psql itself failed (auth, network, SQL error)
        and ValueError if it ran fine but didn't print exactly one number.
        """
        try:
            p = subprocess.run(self.cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               universal_newlines=True, timeout=self.timeout, env=self.env)
        except subprocess.TimeoutExpired:
            raise RuntimeError("psql не уложился в {} с".format(self.timeout))
        if p.returncode != 0:
            # psql errors span several lines (message, LINE 1:, caret); squash them into one
            err = " | ".join(line.strip() for line in p.stderr.splitlines() if line.strip())
            raise RuntimeError("psql вернул код {}: {}".format(p.returncode, err))

        lines = [line.strip() for line in p.stdout.splitlines() if line.strip()]
        if len(lines) != 1 or not NUM_RE.match(lines[0]):
            raise ValueError("ожидалось одно число, получено: {!r}".format(p.stdout[:200]))
        return lines[0]


def write_result(path, value):
    """Overwrite the file in place with the new value.

    Same file, same inode - so owner, mode, ACLs and any Astra security
    labels stay exactly as you set them. We deliberately don't open with
    O_TRUNC: the new number goes over the old one in a single write() and
    only then is the file cut to size, so a reader like the Zabbix agent
    never catches it empty. The one tiny gap left: when the number gets
    shorter, for a few microseconds the tail of the old one is still there.
    """
    data = (value + "\n").encode()
    # 0o644 only matters if the file doesn't exist yet (umask still applies);
    # an existing file keeps whatever permissions it already has.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o644)
    try:
        os.write(fd, data)
        os.ftruncate(fd, len(data))
    finally:
        os.close(fd)


def on_sigterm(signum, frame):
    # Turn `kill <pid>` into a regular exit so the main loop can say goodbye.
    raise SystemExit("получен сигнал {}".format(signum))


def main():
    ap = argparse.ArgumentParser(
        description="Периодически выполняет SQL через psql и пишет число в файл. "
                    "Параметры подключения, не заданные флагами, спрашиваются при старте.")
    ap.add_argument("-s", "--sql", required=True, help="файл с SQL-запросом")
    ap.add_argument("-o", "--out", required=True, help="выходной файл (перезаписывается)")
    ap.add_argument("-i", "--interval", required=True, type=parse_interval,
                    help="интервал: секунды или 30s / 5m / 1h")
    ap.add_argument("-H", "--host", help="хост СУБД (иначе спросит)")
    ap.add_argument("-p", "--port", help="порт (иначе спросит)")
    ap.add_argument("-d", "--dbname", help="имя базы (иначе спросит)")
    ap.add_argument("-U", "--user", help="пользователь СУБД (иначе спросит)")
    ap.add_argument("-t", "--timeout", type=int, default=60,
                    help="таймаут одного запуска psql, сек (по умолчанию 60)")
    ap.add_argument("--once", action="store_true", help="выполнить один раз и выйти (для проверки)")
    ap.add_argument("-v", "--verbose", action="store_true", help="логировать каждое успешное значение")
    args = ap.parse_args()

    # Pin both paths down right away, so it doesn't matter where we're launched from.
    sql_path = os.path.abspath(os.path.expanduser(args.sql))
    out_path = os.path.abspath(os.path.expanduser(args.out))
    out_dir = os.path.dirname(out_path)

    # Check everything we can before asking for a password - no point typing it
    # in only to find out the output folder is missing.
    if not shutil.which("psql"):
        sys.exit("psql не найден в PATH")
    if not os.access(sql_path, os.R_OK):
        sys.exit("не могу прочитать SQL-файл: {}".format(sql_path))
    if not os.path.isdir(out_dir):
        sys.exit("каталог для выходного файла не существует: {}".format(out_dir))
    # An existing file just needs to be writable; a new one needs a writable folder.
    if os.path.exists(out_path):
        if not os.access(out_path, os.W_OK):
            sys.exit("нет прав на запись в файл: {}".format(out_path))
    elif not os.access(out_dir, os.W_OK):
        sys.exit("файла нет, а создать его в {} нет прав".format(out_dir))
    if args.port and not args.port.isdigit():
        sys.exit("порт должен быть числом: {}".format(args.port))

    try:
        host = args.host or ask("Хост СУБД", "localhost")
        port = args.port or ask("Порт", "5432", check=str.isdigit)
        dbname = args.dbname or ask("База")
        user = args.user or ask("Пользователь", getpass.getuser())
        password = getpass.getpass("Пароль для {}@{}: ".format(user, host))
    except (KeyboardInterrupt, EOFError):
        print(file=sys.stderr)
        sys.exit("ввод прерван")

    poller = Poller(host, port, dbname, user, password, sql_path, args.timeout)
    del password

    # Try it once straight away: a typo in the password should stop us here,
    # not turn into an endless stream of errors. An odd result (say, NULL) is
    # fine though - the connection works, so we just keep polling.
    value = None
    try:
        value = poller.query()
    except ValueError as e:
        log("ВНИМАНИЕ: {}; файл пока не записан, продолжаю опрос".format(e))
    except RuntimeError as e:
        sys.exit("первый запрос не прошёл: {}".format(e))

    signal.signal(signal.SIGTERM, on_sigterm)
    log("подключение ОК. sql={} out={} interval={}s (остановка - Ctrl+C)".format(
        sql_path, out_path, args.interval))

    last_error = None
    next_run = time.monotonic()
    try:
        while True:
            if value is not None:
                try:
                    write_result(out_path, value)
                    if args.verbose or args.once:
                        log("OK: {}".format(value))
                except OSError as e:
                    log("ОШИБКА записи в {}: {}".format(out_path, e))

            if args.once:
                return 0 if value is not None else 1

            # Keep a steady rhythm: the interval counts from the start of the
            # previous run, not its end. If a run overshot the interval, start
            # the next one right away instead of trying to catch up.
            next_run += args.interval
            now = time.monotonic()
            if next_run < now:
                next_run = now
            time.sleep(next_run - now)

            # Log an error once, not every interval, and say when it clears up.
            # While it lasts the file keeps the last good number and its mtime
            # stops moving - that's how outside monitoring can tell it's stale.
            try:
                value = poller.query()
                if last_error:
                    log("восстановилось после ошибки: {}".format(value))
                    last_error = None
            except (RuntimeError, ValueError) as e:
                value = None
                if str(e) != last_error:
                    log("ОШИБКА: {}; файл не изменён (повторы не логирую)".format(e))
                    last_error = str(e)
    except (KeyboardInterrupt, SystemExit) as e:
        log("остановка ({})".format(str(e) or "Ctrl+C"))
        return 0


if __name__ == "__main__":
    sys.exit(main())
