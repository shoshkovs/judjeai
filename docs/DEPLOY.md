# JudgeAI: CI/CD и развёртывание на esudya.ru — полная инструкция

Статус: **инструкция к выполнению.** Деплой запускается автоматически только пушем в `main` через GitHub Actions; ручной деплой на сервере — только по процедурам отката/восстановления из этого документа.

Действующая конфигурация: пуш в `main` → workflow GitHub Actions `.github/workflows/ci.yml` (тесты → сборка образов в `ghcr.io/shoshkovs/judjeai` → деплой на сервер по SSH) → сайт обновляется сам. Изменения на сервере руками не применяются.

> **Переезд с GitLab на GitHub (28.10.2026).** Репозиторий: https://github.com/shoshkovs/judjeai (GitLab `judgeai/judgeai` заморожен). Подготовка сервера (этап B) не меняется. Вместо шагов A.5 (deploy token GitLab), B.6 (логин в реестр GitLab) и этапа C (переменные GitLab CI) действует раздел «GitHub Actions» ниже. Упоминания GitLab, kaniko и `registry.gitlab.com` в остальных разделах — история прежней схемы.

## GitHub Actions: секреты и первый деплой

1. **Секреты репозитория** — github.com/shoshkovs/judjeai → **Settings → Secrets and variables → Actions → New repository secret**:

   | Name | Value |
   |---|---|
   | `DEPLOY_HOST` | `esudya.ru` |
   | `DEPLOY_USER` | `deploy` |
   | `SSH_PRIVATE_KEY` | всё содержимое приватного ключа деплоя (как в A.6/B.7), включая строки BEGIN/END |
   | `SSH_KNOWN_HOSTS` | вывод `ssh-keyscan -H esudya.ru` (C.1) |

   Значения секретов GitHub маскирует в логах; workflow пишет ключ в файл, не выводя его, и выравнивает CRLF/последний перевод строки. Если ключ когда-либо попадал в лог (инцидент T30) — сначала выпустить новую пару (B.7).
2. **Окружение `production`** создаётся при первом деплое само; при желании в **Settings → Environments → production** можно ограничить его веткой `main` и включить ручное подтверждение деплоя.
3. **Реестр образов.** Сборка пушит `ghcr.io/shoshkovs/judjeai/api` и `/web` токеном workflow. Постоянный логин сервера в реестр не нужен: на время деплоя workflow выполняет на сервере `docker login ghcr.io` одноразовым токеном этого запуска и после `pull` делает `docker logout`. Старый логин в `registry.gitlab.com` на сервере можно удалить (`docker logout registry.gitlab.com`).
4. **Первый деплой** — любой пуш (или слияние PR) в `main`. Порядок: `inventory`, `api-tests`, `browser-auth` → `build` (api и web параллельно) → `deploy` с health-check `https://esudya.ru/health`. Pull request и прочие ветки гоняют только тесты.
5. **Откат** — Actions → нужный зелёный запуск на `main` → **Re-run jobs** для `deploy`: workflow развернёт образы того коммита (тег — первые 8 символов SHA).

Минуты: для публичного репозитория стандартные runner'ы GitHub Actions бесплатны без месячной квоты.

---

## 0. Как читать этот документ

В работе три роли:

| Роль | Где работает | Что делает |
|---|---|---|
| 🧑 **Человек** | Свой компьютер, панель регистратора домена, gitlab.com | DNS, начальная настройка VPS, секреты, переменные GitLab CI, наблюдение за pipeline |
| 🤖 **Агент** | Терминал на VPS (пользователь `deploy`) | Безопасность сервера, Docker, каталог приложения, диагностика, бэкапы, откат |
| ⚙️ **GitLab CI** | gitlab.com | Тесты, сборка Docker-образов, автодеплой через SSH |

Порядок этапов (каждый следующий начинается, когда предыдущий подтверждён):

1. **Этап A** (человек): DNS-записи, первый вход на сервер, создание пользователя `deploy`, генерация секретов, ключи.
2. **Этап B** (агент на VPS): настройка сервера с нуля — обновления, SSH-харднинг, файрвол, fail2ban, Docker, каталог `/opt/judgeai`.
3. **Этап C** (человек): переменные в GitLab CI/CD.
4. **Этап D** (агент + человек): первый деплой пушем в `main` и приёмка.
5. **Этап E** (агент): регулярные операции — бэкапы, мониторинг, откат.

**Агенту: часть B — твоя. Части A, C выполняет человек со своего компьютера, часть D вы делаете вместе.** Инструкция для агента — раздел 2 (этап B); она написана так, чтобы её можно было выполнять сверху вниз.

### Архитектура после настройки

```
Интернет ──▶ esudya.ru (VPS, Ubuntu 24.04)
              │
              ▼
         [caddy :80/:443]        — TLS-сертификат Let's Encrypt, авто-продление
              │
        ┌─────┴──────────┐
        ▼                ▼
   /api/*, /health   остальные пути
        │                │
        ▼                ▼
  [api :8000]      [web :80, nginx]
  FastAPI+uvicorn   статика verdikt-web (React, сборка CI)
        │
        ▼
  [db :5432] pgvector/pgvector:pg16 — наружу НЕ опубликован
```

Что происходит при `git push` в `main`:

1. **test** — существующие проверки: `inventory`, `api-tests` (PostgreSQL в CI), `browser-auth` (Playwright). Деплой не начнётся, пока не зелёные.
2. **build** — kaniko собирает два образа и пушит их в реестр GitLab (`registry.gitlab.com/judgeai/judgeai`):
   - `api` — из корневого `Dockerfile` (FastAPI);
   - `web` — из `verdikt-web/Dockerfile` (nginx + статическая сборка React).
   Теги: короткий SHA коммита и `latest`.
3. **deploy** — джоба подключается по SSH к `deploy@esudya.ru`:
   - копирует `deploy/docker-compose.prod.yml`, `deploy/Caddyfile` и файл `deploy.env` (точные теги образов) в `/opt/judgeai/`;
   - выполняет `docker compose pull && docker compose up -d`;
   - контейнер `migrate` один раз прогоняет `alembic upgrade head`, затем стартует `api`;
   - джоба ждёт ответа `https://esudya.ru/health` (до 10 минут) и завершает pipeline зелёным.

Файлы CI/CD в репозитории:

| Файл | Назначение |
|---|---|
| `.gitlab-ci.yml` | Пайплайн: test → build → deploy; джобы build/deploy — только для `main` |
| `Dockerfile` | Образ `api` |
| `verdikt-web/Dockerfile`, `verdikt-web/nginx.conf` | Образ `web` |
| `deploy/docker-compose.prod.yml` | Прод-стек сервера (caddy, web, migrate, api, db) |
| `deploy/Caddyfile` | Роутинг и TLS для esudya.ru |

Требования к VPS (минимум): Ubuntu 24.04 LTS, 2 vCPU, 2 ГБ RAM + swap, 20 ГБ диска. Рекомендуется 4 ГБ RAM: эмбеддинги считаются на CPU, при деплое короткий всплеск памяти. Для пилота с персональными данными приговоров сервер в дата-центре в РФ — обязательное предпочтение (152-ФЗ).

---

## 1. Этап A — подготовка (человек, со своего компьютера)

### A.1. DNS-записи

В панели регистратора домена `esudya.ru` создайте записи (IP замените на реальный адрес VPS, он приходит в письме от хостера):

| Тип | Имя | Значение | TTL |
|---|---|---|---|
| A | `@` | `IP_СЕРВЕРА` | 3600 |
| A | `www` | `IP_СЕРВЕРА` | 3600 |

Проверка (может занять до нескольких часов, обычно минуты):

```bash
dig +short esudya.ru A
dig +short www.esudya.ru A
```

Обе команды должны вернуть IP сервера (если `dig` нет на вашем компьютере — `nslookup esudya.ru`). Пока DNS не отвечает правильно — дальше не идём: Caddy не сможет выпустить TLS-сертификат.

### A.2. Личный SSH-ключ (если ещё нет)

```bash
ls ~/.ssh/id_ed25519.pub 2>/dev/null || ssh-keygen -t ed25519 -C "personal"
cat ~/.ssh/id_ed25519.pub
```

### A.3. Первый вход на сервер и создание пользователя `deploy`

IP и root-пароль приходят от хостера (или задаются в панели VPS). Подключитесь от root:

```bash
ssh root@IP_СЕРВЕРА
```

Выполните (подставьте свой публичный ключ из A.2 вместо `<ПУБЛИЧНЫЙ_КЛЮЧ>`). Если `sudo: command not found` — минимальный образ без sudo, сначала `apt update && apt install -y sudo`:

```bash
apt update && apt install -y sudo
adduser --gecos "" deploy
usermod -aG sudo deploy
mkdir -p /home/deploy/.ssh
echo "<ПУБЛИЧНЫЙ_КЛЮЧ>" > /home/deploy/.ssh/authorized_keys
chown -R deploy:deploy /home/deploy/.ssh
chmod 700 /home/deploy/.ssh
chmod 600 /home/deploy/.ssh/authorized_keys
exit
```

`adduser` спросит пароль для `deploy` — запишите его: это пароль для `sudo` и для входа, пока не выключим парольный вход на этапе B.

Проверьте, что вход по ключу и sudo работают (в новом окне терминала):

```bash
ssh deploy@IP_СЕРВЕРА
sudo -v        # спросит пароль deploy — должен принять
exit
```

Дальше на сервере работает AI-агент (этап B): зайдите `ssh deploy@IP_СЕРВЕРА`, запустите агента в `tmux`-сессии и передайте ему раздел 2 (этап B) этого документа.

### A.4. Секреты для production `.env`

Сгенерируйте на своём компьютере (bash/macOS):

```bash
openssl rand -base64 24     # → POSTGRES_PASSWORD
```

Определитесь с LLM-провайдером и подготовьте три значения: `LLM_API_BASE`, `LLM_API_KEY`, `LLM_MODEL` (например, OpenAI-совместимый API). Если ключа пока нет — оставьте `LLM_PROVIDER=fake` (система заработает, анализ будет возвращать демонстрационный шаблон; сменить можно позже в `/opt/judgeai/.env` и перезапустить стек).

Передайте агенту вместе с инструкцией: сгенерированный `POSTGRES_PASSWORD`, настройки LLM. Это единственный канал для секретов: лично в сессию агента. Не коммитить, не отправлять в git, не вставлять в issues/MR.

### A.5. Deploy token для реестра GitLab

Серверу нужно скачивать образы из приватного реестра `registry.gitlab.com`.

На gitlab.com: **Project `judgeai/judgeai` → Settings → Repository → Deploy tokens → Add token**:

- Token name: `vps-esudya`
- Scopes: `read_registry`
- Username: сгенерируется вида `gitlab+deploy-token-123`, токен — длинная строка. **Оба значения покажите один раз** — токен больше не увидеть, только пересоздать.

Передайте пару (username, token) агенту на этапе B.6.

### A.6. Отдельный SSH-ключ, которым GitLab CI будет деплоить

На своём компьютере:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/judgeai_deploy -N "" -C "judgeai-ci-deploy"
cat ~/.ssh/judgeai_deploy.pub
```

Публичный ключ передайте агенту (раздел B.7 — он добавит его в `authorized_keys` на сервере). Приватный файл `~/.ssh/judgeai_deploy` понадобится вам на этапе C — не удаляйте его и не публикуйте.

---

## 2. Этап B — настройка сервера с нуля (агент на VPS)

> **Это твоя часть.** Ты работаешь на сервере esudya.ru под пользователем `deploy` (sudo по паролю). Выполняй разделы B.1–B.12 строго по порядку; после каждого — выполняй команду «Проверка» и сверяй ожидаемый результат. Всё, что не описано в этом разделе, не делай.
>
> **Правила безопасности (обязательны):**
> - Не открывай наружу порты кроме 22, 80, 443. БД и API не публикуются в docker-compose — так и должно быть.
> - Не записывай секреты (пароли, токены, ключи) в git, в файлы вне `/opt/judgeai/.env` и `/home/deploy/.docker/`, в логи и свои отчёты.
> - Не удаляй и не «чисти» docker-тома (`pgdata`, `uploads`, `caddy_data`, `model_cache`) — в них база данных, файлы дел и TLS-сертификаты.
> - После этапа B не разворачивай и не обновляй приложение руками — это делает только GitLab CI через `deploy`-джобу. Твои операции — бэкапы, диагностика, откат по разделу 5.4.
> - Если проверка шага не прошла и причина не очевидна — остановись, зафиксируй вывод команды и попроси человека. Не «чини» вслепую.
> - Каждое изменение конфигурации SSH выполняй так: применил → проверил в НОВОМ терминале, не закрывая текущую сессию → только потом продолжай. Иначе можно потерять доступ к серверу.

### B.1. Сбор информации

```bash
command -v dig >/dev/null || sudo apt install -y dnsutils   # dig есть не во всех образах
whoami                      # ожидание: deploy
sudo -v                     # ввод пароля; ошибок быть не должно
lsb_release -ds             # ожидание: Ubuntu 24.04.x LTS
uname -m                    # ожидание: x86_64 (или aarch64 — сообщи человеку)
ip -4 addr show scope global | grep inet
df -h /
free -h
dig +short esudya.ru A      # ожидание: публичный IP этого же сервера
```

`dig` должен вернуть тот же адрес, что показал `ip addr` (может отличаться, если сервер за NAT — тогда сверься с человеком). Если DNS пустой или указывает на другой адрес — **стоп**: попроси человека доделать A.1.

### B.2. Обновление системы

```bash
sudo apt update && sudo apt full-upgrade -y
sudo reboot
```

После перезагрузки перезапусти свою сессию и проверь:

```bash
uptime
sudo apt update    # 0 packages to upgrade
```

### B.3. Время и таймзона

```bash
sudo timedatectl set-timezone Europe/Moscow
timedatectl status    # ожидание: System clock synchronized: yes, NTP service: active
```

### B.4. Swap (пропусти, если RAM ≥ 4 ГБ)

```bash
free -h | grep -i swap    # если Swap уже не 0B — пропусти этот шаг
sudo fallocate -l 4G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
cat /proc/sys/vm/swappiness    # если > 10: echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-swap.conf && sudo sysctl -p /etc/sysctl.d/99-swap.conf
free -h    # ожидание: Swap 4.0Gi
```

### B.5. Docker Engine

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
```

Перевойди в сессию (или `newgrp docker`) — членство в группе `docker` вступает только в новой сессии. Проверка:

```bash
docker run --rm hello-world    # ожидание: "Hello from Docker!"
docker compose version         # ожидание: Docker Compose version v2.x
```

Если `hello-world` не скачивается (таймаут, `toomanyrequests`, network error) — вероятная причина: недоступность Docker Hub с российских IP. Тогда:

```bash
sudo nano /etc/docker/daemon.json
```

содержимое (актуальный зеркальный сервер уточни по сети и подставь; пример структуры):

```json
{
  "registry-mirrors": ["https://<актуальное-зеркало>"]
}
```

```bash
sudo systemctl restart docker
docker run --rm hello-world
```

Если зеркало не помогло — сообщи человеку: потребуется вариант доставки базовых образов (caddy, pgvector) через реестр GitLab или импорт `docker save/load`. Собственные образы проекта (`registry.gitlab.com/...`) с серверов РФ обычно доступны без ограничений.

### B.6. Доступ к реестру GitLab

Возьми у человека deploy token (раздел A.5): username вида `gitlab+deploy-token-N` и токен.

```bash
echo "<ТОКЕН>" | docker login registry.gitlab.com -u "<USERNAME>" --password-stdin
```

Проверка:

```bash
grep -c registry.gitlab.com ~/.docker/config.json   # ожидание: не 0
```

(строка в `auths` — логин сохранился; сам токен из отчётов не цитируй).

### B.7. Добавить CI-ключ в authorized_keys

Человек передал публичный ключ из A.6 (одна строка `ssh-ed25519 AAAA... judgeai-ci-deploy`):

```bash
echo "<ПУБЛИЧНЫЙ_КЛЮЧ_CI>" >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
wc -l ~/.ssh/authorized_keys   # ожидание: 2 строки (личный ключ человека + CI-ключ); если больше — сверь состав с человеком
```

### B.8. SSH-харднинг

В OpenSSH для каждого параметра побеждает **первое** прочитанное значение, а файлы из `/etc/ssh/sshd_config.d/` читаются в алфавитном порядке раньше основного конфига. Поэтому наш файл должен иметь префикс `00-`, чтобы перекрыть возможные настройки cloud-init (`50-cloud-init.conf` и т.п.).

Сначала посмотри, что уже настроено:

```bash
grep -rsE "PasswordAuthentication|PermitRootLogin|KbdInteractive" /etc/ssh/sshd_config /etc/ssh/sshd_config.d/ || echo "явных настроек нет"
```

Затем попроси человека подтвердить, что вход по его личному ключу работает (п. A.3 он уже проверял; лишний раз — не помешает). После подтверждения:

```bash
sudo tee /etc/ssh/sshd_config.d/00-hardening.conf > /dev/null <<'EOF'
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
X11Forwarding no
MaxAuthTries 4
LoginGraceTime 30
AllowUsers deploy
EOF
sudo sshd -t    # ожидание: пустой вывод (синтаксис ок)
```

Проверь **эффективные** значения (то, что реально применит sshd):

```bash
sudo sshd -T | grep -E '^(permitrootlogin|passwordauthentication|kbdinteractiveauthentication|allowusers) '
# ожидание:
# permitrootlogin no
# passwordauthentication no
# kbdinteractiveauthentication no
# allowusers deploy
```

Если `passwordauthentication` всё ещё `yes` — какой-то файл читается раньше; найди его (`grep -rs PasswordAuthentication /etc/ssh/`) и закомментируй строку. Только после этого:

```bash
sudo systemctl restart ssh
```

**Не закрывай текущую сессию.** Попроси человека в новом окне терминала на его компьютере выполнить две проверки:

```bash
ssh deploy@esudya.ru                        # вход по ключу работает
ssh root@esudya.ru                          # ожидание: Permission denied (publickey)
```

Человек подтвердил обе — харднинг завершён, сессию можно закрыть. Если человек сообщает, что вход по ключу сломался — верни конфиг обратно в этой же сессии:

```bash
sudo rm /etc/ssh/sshd_config.d/00-hardening.conf && sudo systemctl restart ssh
```

### B.9. Файрвол

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw --force enable
sudo ufw status verbose    # ожидание: активен; разрешены 22, 80, 443/tcp
```

Важно: Docker публикует порты контейнеров в обход правил ufw. В нашем прод-стеке наружу опубликованы **только** 80/443 у caddy; `db` и `api` портов не публикуют. Не добавляй `ports:` сервисам db/api в `docker-compose.prod.yml`.

### B.10. fail2ban и автообновления безопасности

```bash
sudo apt install -y fail2ban
sudo systemctl enable --now fail2ban
sudo fail2ban-client status sshd    # ожидание: jail листинг с "Currently banned: 0"

sudo apt install -y unattended-upgrades
sudo dpkg-reconfigure -plow unattended-upgrades    # выбрать "Yes"
systemctl status apt-news.timer unattended-upgrades.timer --no-pager | grep -c active   # ожидание: не 0
```

### B.11. Каталог приложения и `.env`

```bash
sudo mkdir -p /opt/judgeai/backups /opt/judgeai/scripts
sudo chown -R deploy:deploy /opt/judgeai
nano /opt/judgeai/.env
```

Содержимое — значения от человека из A.4 (шаблон в Приложении A, `nano` вставка: `Ctrl+O`, `Enter`, `Ctrl+X`):

```bash
chmod 600 /opt/judgeai/.env
ls -la /opt/judgeai/    # ожидание: .env с правами -rw------- deploy deploy
```

### B.12. Отчёт о готовности

Проверь и доложи человеку списком:

```bash
sudo ufw status | head -5
sudo fail2ban-client status sshd | grep "Currently banned"
docker --version && docker compose version
docker login registry.gitlab.com 2>&1 | grep -o "Login Succeeded" || echo "already logged in"
ls -la /opt/judgeai/
dig +short esudya.ru A
```

Формулировка для человека: «Сервер готов. Настрой переменные GitLab CI (раздел 3) и сделай push в main. После зелёного pipeline возвращайся на приёмку (раздел 4)».

---

## 3. Этап C — переменные GitLab CI (человек)

Выполняется, когда агент доложит, что сервер готов (раздел 2 завершён отчётом B.12).

### C.1. Снять отпечатки хост-ключей сервера

```bash
ssh-keyscan -H esudya.ru > /tmp/known_hosts
wc -l /tmp/known_hosts     # не должен быть пустым
cat /tmp/known_hosts
```

### C.2. Создать переменные

gitlab.com: **Project → Settings → CI/CD → Variables → Add variable**. Каждую добавлять отдельно:

| Key | Type | Value | Protect | Mask |
|---|---|---|---|---|
| `DEPLOY_HOST` | Variable | `esudya.ru` | ✔ | ✔ |
| `DEPLOY_USER` | Variable | `deploy` | ✔ | ✔ |
| `SSH_PRIVATE_KEY` | **File** | содержимое файла `~/.ssh/judgeai_deploy` | ✔ | — |
| `SSH_KNOWN_HOSTS` | **File** | содержимое `/tmp/known_hosts` | ✔ | — |

Для File-переменных: `cat` файл и вставьте всё содержимое, включая первую и последнюю строки. Mask для File-переменных недоступен в GitLab — это нормально: protected ограничивает доступ переменной защищёнными ветками, а `main` защищён по умолчанию.

### C.3. Убедиться, что `main` — защищённая ветка

**Settings → Repository → Protected branches**: `main` должен быть в списке (по умолчанию так и есть). Иначе protected-переменные не подставятся в pipeline.

### C.4. Запустить первый деплой

```bash
git add .gitlab-ci.yml Dockerfile verdikt-web/Dockerfile verdikt-web/nginx.conf verdikt-web/.dockerignore deploy/ docs/DEPLOY.md docs/TASKS.md
git commit -m "CI/CD: build images and deploy to esudya.ru"
git push origin main
```

Наблюдение: **CI/CD → Pipelines**. Порядок джоб: `inventory`, `api-tests`, `browser-auth` (параллельно) → `build-api`, `build-web` (параллельно) → `deploy`. Полное время — примерно 20–40 минут. Джоба `deploy` должна завершиться `Health check passed`.

Квота: на бесплатном тарифе gitlab.com доступно ~400 вычислительных минут общих runner'ов в месяц; один полный pipeline занимает до ~40 минут. Если quota исчерпана — pipeline зависнет в `pending`. Варианты: слить несколько коммитов в один пуш, либо поставить собственный runner на VPS (Settings → CI/CD → Runners, регистрация по официальной инструкции https://docs.gitlab.com/runner/install/; executor `docker`). Тесты тогда будут гоняться на самом VPS — учитывайте нагрузку.

Когда pipeline зелёный — переходите к этапу D (приёмка, раздел 4).

---

## 4. Этап D — первый деплой и приёмка

### D.1. Человек

1. Выполни раздел 3 (переменные GitLab) и push в `main` (раздел C.4).
2. Дождись зелёного pipeline; джоба `deploy` должна закончиться строкой `Health check passed: https://esudya.ru/health`.
3. Открой в браузере `https://esudya.ru` — лендинг должен открыться по HTTPS (сертификат выпускается Caddy автоматически при первом запуске, до ~1–2 минут).
4. Создай первую учётную запись (команда в D.2) и вместе с агентом проведи финальную приёмку.

### D.2. Агент: приёмка на сервере

Выполни после зелёного `deploy`:

```bash
cd /opt/judgeai
docker compose --env-file deploy.env -f docker-compose.prod.yml ps
```

Ожидание: `judgeai_caddy`, `judgeai_web`, `judgeai_api`, `judgeai_db` — `Up` (`api` — `healthy` через ~минуту), `judgeai_migrate` — `Exited (0)`.

```bash
curl -fsS https://esudya.ru/health        # {"status":"ok"}
curl -fsI https://esudya.ru | head -3     # HTTP/2 200
docker logs --tail 20 judgeai_caddy 2>&1 | grep -iE "error|acme" || echo "caddy: чисто"
```

Создание первого пользователя (команда интерактивная, пароль вводится скрыто; укажи человеку, что делать это должен он сам — пароль не должен проходить через агента):

```bash
docker compose --env-file deploy.env -f docker-compose.prod.yml exec api \
  python -m src.cli.users create --username <ИМЯ> --admin
```

Финальная проверка в браузере (вместе с человеком): вход на `https://esudya.ru/app`, создание тестового дела с текстом, появление результата. Проверка загрузки файла: любой `.txt` → 202 → анализ завершается.

### D.3. Если что-то не работает

Порядок диагностики:

1. Джоба `deploy` в GitLab (CI/CD → Pipelines → последняя джоба) — на каком шаге красная: `scp`/`ssh` (ключи/переменные), `pull` (docker login, квота реестра), `up` (смотри пункт 2), health-check (смотри пункт 2).
2. На сервере: `docker compose --env-file deploy.env -f docker-compose.prod.yml ps`, затем `docker logs --tail 50 judgeai_migrate`, `judgeai_api`, `judgeai_caddy`, `judgeai_db` — по одному.
3. `dig +short esudya.ru A` и `sudo ufw status`.
4. Сертификат: `docker logs judgeai_caddy 2>&1 | grep -i "certificate obtained"` — если пусто, значит ACME не прошёл (DNS/порты 80/443).
5. Дальше — Приложение C (частые проблемы). Не помогло — зафиксируй выводы и передай человеку.

---

## 5. Этап E — регулярные операции (агент)

### 5.1. Обновления приложения

Ничего не делать. Пуш в `main` → CI сам соберёт и задеплоит. Серверных ручных действий нет. Проверить успешность: `docker compose --env-file deploy.env -f docker-compose.prod.yml ps` и `curl -fsS https://esudya.ru/health`; текущий SHA коммита виден в тегах образов: `grep IMAGE /opt/judgeai/deploy.env`.

### 5.2. Логи

```bash
docker logs --tail 100 judgeai_api
docker logs -f judgeai_api        # поток
docker system df                  # место
```

В логах не должно быть текстов приговоров и ФИО — только идентификаторы и статусы. Если заметишь иное — сообщи человеку (это регрессия приватности).

### 5.3. Бэкапы (настроить один раз)

Создай скрипт `/opt/judgeai/scripts/backup.sh` (шаблон — Приложение B) и cron:

```bash
chmod +x /opt/judgeai/scripts/backup.sh
crontab -e
```

добавить строку:

```
30 3 * * * /opt/judgeai/scripts/backup.sh >> /opt/judgeai/backups/backup.log 2>&1
```

Проверка (запусти вручную, затем):

```bash
/opt/judgeai/scripts/backup.sh
ls -lh /opt/judgeai/backups/*.dump | tail -3
```

Ротация: хранить 14 последних дампов (скрипт делает сам). Восстановление проверять по Приложению B не реже раза в месяц и по требованию человека. Копия дампов вне сервера (S3/другая машина) — открытый пункт, решение за человеком.

### 5.4. Откат на предыдущую версию

Способ 1 (основной): в GitLab CI/CD → Pipelines открыть **последний зелёный** pipeline нужного коммита → джоба `deploy` → кнопка **Retry**. Стек перейдёт на образы того коммита.

Способ 2 (вручную, если CI недоступен):

```bash
cd /opt/judgeai
nano deploy.env    # заменить SHA в API_IMAGE и WEB_IMAGE на SHA нужного коммита
docker compose --env-file deploy.env -f docker-compose.prod.yml pull
docker compose --env-file deploy.env -f docker-compose.prod.yml up -d
```

SHA коммитов видны в GitLab (Project → Commits), теги образов — в Container Registry. Откат миграций БД (`alembic downgrade`) без бэкапа и указания человека не выполнять.

### 5.5. Мониторинг (настроить один раз)

Создай `/opt/judgeai/scripts/health-check.sh` (шаблон — Приложение B) и cron каждые 5 минут (`*/5 * * * * ...`). Лог — `/opt/judgeai/health.log`; после двух отказов подряд создаётся маркер `/opt/judgeai/ALERT-health` (убрать после разбора). В каждой сессии на сервере проверяй его наличие первым делом.

---

## 6. Приложения

### Приложение A — шаблон `/opt/judgeai/.env` (production)

Значения `<...>` — от человека (A.4). Остальное — как указано:

```bash
POSTGRES_USER=judgeai
POSTGRES_PASSWORD=<сгенерированная строка>
POSTGRES_HOST=db
POSTGRES_PORT=5432
POSTGRES_DB=judgeai

UPLOAD_DIR=/app/uploads
UPLOAD_MAX_MB=50

LLM_PROVIDER=openai
LLM_API_BASE=<URL OpenAI-совместимого API>
LLM_API_KEY=<ключ>
LLM_MODEL=<модель>

EMBEDDING_MODEL=sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
EMBEDDING_DIM=384
EMBEDDING_CACHE_DIR=/app/.fastembed_cache
EMBEDDING_CHUNK_SIZE=2500
EMBEDDING_MAX_CHUNKS=8

AUTH_COOKIE_SECURE=true
CORS_ORIGINS=https://esudya.ru,https://www.esudya.ru
```

Примечания: `LLM_PROVIDER=fake` — только для демонстрации без реального ключа; `AUTH_COOKIE_SECURE=true` обязателен для HTTPS; `CORS_ORIGINS` в проде фактически не участвует (фронт и API на одном домене), но задаётся явно.

### Приложение B — скрипты на сервере

`/opt/judgeai/scripts/backup.sh`:

```bash
#!/bin/sh
set -eu
cd /opt/judgeai
PGUSER=$(grep -E '^POSTGRES_USER=' .env | tail -n1 | cut -d= -f2-)
PGDB=$(grep -E '^POSTGRES_DB=' .env | tail -n1 | cut -d= -f2-)
STAMP=$(date +%Y%m%d_%H%M%S)
OUT="backups/judgeai_${STAMP}.dump"
docker compose --env-file deploy.env -f docker-compose.prod.yml exec -T db \
  pg_dump -U "$PGUSER" -Fc "$PGDB" > "$OUT"
ls -1t backups/judgeai_*.dump | tail -n +15 | xargs -r rm --
echo "$(date -Is) backup ok: $OUT ($(du -h "$OUT" | cut -f1))"
```

Проверка восстановления (в отдельную тестовую БД, прод не трогает):

```bash
cd /opt/judgeai
PGUSER=$(grep -E '^POSTGRES_USER=' .env | tail -n1 | cut -d= -f2-)
PGDB=$(grep -E '^POSTGRES_DB=' .env | tail -n1 | cut -d= -f2-)
LATEST=$(ls -1t backups/judgeai_*.dump | head -1)
docker compose --env-file deploy.env -f docker-compose.prod.yml exec -T db \
  psql -U "$PGUSER" -d postgres -c "DROP DATABASE IF EXISTS judgeai_restore_test;"
docker compose --env-file deploy.env -f docker-compose.prod.yml exec -T db \
  createdb -U "$PGUSER" judgeai_restore_test
cat "$LATEST" | docker compose --env-file deploy.env -f docker-compose.prod.yml exec -T db \
  pg_restore -U "$PGUSER" -d judgeai_restore_test --no-owner
docker compose --env-file deploy.env -f docker-compose.prod.yml exec -T db \
  psql -U "$PGUSER" -d judgeai_restore_test -c "SELECT count(*) FROM chats;"
```

`/opt/judgeai/scripts/health-check.sh`:

```bash
#!/bin/sh
set -u
LOG=/opt/judgeai/health.log
MARKER=/opt/judgeai/ALERT-health
TS=$(date -Is)
if curl -fsS --max-time 15 https://esudya.ru/health >/dev/null 2>&1; then
  echo "$TS ok" >> "$LOG"
  rm -f "$MARKER"
else
  echo "$TS FAIL" >> "$LOG"
  PREV=$(tail -2 "$LOG" | head -1 | awk '{print $2}')
  [ "$PREV" = "FAIL" ] && touch "$MARKER"
fi
tail -1000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
```

### Приложение C — частые проблемы

| Симптом | Причина | Действие |
|---|---|---|
| `deploy` красная на `scp`/`ssh`: Permission denied | CI-ключ не в `authorized_keys`, неверные `DEPLOY_HOST/USER`, `SSH_KNOWN_HOSTS` снят до смены IP | Проверить B.7; человек перечитывает C.1/C.2 (keyscan после стабилизации DNS) |
| `deploy` красная: `must be CI/CD variables of type File` | `SSH_PRIVATE_KEY`/`SSH_KNOWN_HOSTS` заведены типом Variable (или не заданы) | Пересоздать типом **File**. Если ключ хоть раз попал в лог job — считать скомпрометированным: убрать из `authorized_keys`, стереть лог (Erase job log), выпустить новую пару (B.7) |
| `deploy` красная на `pull`: unauthorized | deploy token истёк/отозван | Человек создаёт новый (A.5), агент повторяет B.6 |
| `migrate` падает | Ошибка миграции/БД | `docker logs judgeai_migrate`; сообщить человеку; откат 5.4 не выполняет миграции назад |
| Caddy: `acme` ошибки, сайт по HTTP без сертификата | DNS не на этот сервер, порты 80/443 закрыты | B.1, `sudo ufw status`; дождаться DNS |
| `docker compose up` висит в pull | Docker Hub недоступен | B.5 (зеркало) |
| Pipeline в GitLab `pending` | Исчерпана квота минут | Раздел 3, конец (квота) |
| 502/504 на сайте | `api` не поднялся | `docker compose ps`, `docker logs judgeai_api`; возможно migrate ещё работает — подождать 2 минуты |
| Сайт работает, но не пускает после входа | `AUTH_COOKIE_SECURE` не `true` | Проверить `/opt/judgeai/.env`, затем `docker compose ... up -d` (перезапуск) |
| На сервере `sudo: command not found`, `su: Authentication failure` | Минимальный образ без пакета sudo; `su -` запрашивает пароль **root**, а не deploy (учётка root часто заблокирована в облачных образах) | Не переустанавливать ОС. Получить root: `ssh root@IP_СЕРВЕРА` со своего компьютера или веб-консоль/сброс пароля root в панели хостера. Затем от root: `apt update && apt install -y sudo && usermod -aG sudo deploy` (при необходимости `passwd deploy`), перевойти как deploy и проверить `sudo -v` |

### Приложение D — карта файлов сервера

```
/home/deploy/
  .ssh/authorized_keys      # личный ключ человека + CI-ключ
  .docker/config.json       # логин в registry.gitlab.com
/opt/judgeai/
  .env                      # секреты прод (создан на этапе B.11, не в git)
  deploy.env                # теги образов текущего релиза (пишет CI)
  docker-compose.prod.yml   # прод-стек (копирует CI из репозитория)
  Caddyfile                 # роутинг/TLS (копирует CI из репозитория)
  backups/                  # pg_dump-архивы + backup.log
  scripts/                  # backup.sh, health-check.sh
  health.log, ALERT-health  # мониторинг
/var/lib/docker/volumes/
  judgeai_pgdata            # база данных — НЕ удалять
  judgeai_uploads           # загруженные файлы дел — НЕ удалять
  judgeai_caddy_data        # TLS-сертификаты — НЕ удалять (rate limit ACME)
  judgeai_model_cache       # кеш модели эмбеддингов
```

---

## 7. Что осталось за рамками (зарегистрировано, не реализовано)

- `/health` не проверяет соединение с БД (проверяет только живость процесса api).
- Контейнеры выполняются от root (внутри Docker); понижение до `USER app` — отдельное изменение Dockerfile с проверкой владельцев томов.
- Копия бэкапов вне сервера не настроена.
- Ограничение частоты запросов, распределённый fail2ban по логам приложения, WAF — не настроены.
- Требования T17 (privacy-аудит до загрузки реальных материалов) остаются обязательными: не загружать приватные образцы приговоров до его закрытия.

Статусы задач и результаты проверок — в [TASKS.md](TASKS.md).
