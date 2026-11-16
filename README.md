# AmneziaWG Profile Manager

## 1. Подключись к новому VPS и установи AmneziaWG

На Mac подставь IP нового VPS и подключись по SSH. При первом входе сверь отпечаток SSH-ключа с данными из консоли провайдера и подтверди его.

```sh
VPS=IP_НОВОГО_VPS
ssh "root@${VPS}"
```

Введи `exit`, чтобы вернуться на Mac. Открой AmneziaVPN, выбери **Self-hosted VPN**, введи IP VPS, пользователя `root` и пароль или ключ. Выбери автоматическую установку AmneziaWG. Когда установка закончится, нажми «Подключиться» в приложении и проверь, что VPN работает. [Инструкция Amnezia](https://docs.amnezia.org/documentation/instructions/install-vpn-on-server/)

## 2. Скачай и запусти серверный контейнер

С Mac снова подключись к VPS:

```sh
ssh "root@${VPS}"
```

На VPS посмотри имя запущенного VPN-контейнера:

```sh
docker ps --format '{{.Names}}'
```

В следующем блоке подставь это имя в `AWG_CONTAINER`. После установки через AmneziaVPN оно обычно `amnezia-awg2`. Если в списке `amnezia-awg`, укажи его.

```sh
AWG_CONTAINER=amnezia-awg2
IMAGE=dvononintech/awg-profile-server:1.0.1
docker pull "$IMAGE" &&
docker run -d --restart unless-stopped --name awg-profile-manager \
  -e "AWG_CONTAINER=$AWG_CONTAINER" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  "$IMAGE"

docker ps --filter name=awg-profile-manager
```

Когда `awg-profile-manager` покажет статус `Up`, введи `exit`. Серверный контейнер продолжит работать на VPS.

Если VPN смонтировал `awg0.conf` только для чтения, посмотри путь к файлу на хосте и флаг записи:

```sh
docker inspect --format '{{range .Mounts}}{{println .Source .Destination .RW}}{{end}}' "$AWG_CONTAINER"
```

Если рядом с `awg0.conf` стоит `false`, добавь в команду запуска менеджера перед `"$IMAGE"` два параметра. В `source` подставь путь из первого столбца:

```sh
--mount type=bind,source=/ПУТЬ/К/awg0.conf,target=/awg0.conf \
-e AWG_WRITABLE_CONFIG_PATH=/awg0.conf
```

Менеджер проверит, что это тот же файл, который использует VPN.

## 3. Скопируй репозиторий и скачай клиент на Mac

Запусти Docker Desktop. Склонируй репозиторий с GitHub на Mac:

```sh
git clone git@github.com:dvoronin-tech/manage-profiles-awg.git "$HOME/awg-profile-manager"
cd "$HOME/awg-profile-manager"
```

Теперь скачай клиентский образ и укажи его для скрипта:

```sh
export AWG_CLIENT_IMAGE=dvononintech/awg-profile-client:1.0.0
docker pull "$AWG_CLIENT_IMAGE"
```

Команды ниже выполняй в той же папке и в том же терминале. В новом терминале заново задай `VPS` и `AWG_CLIENT_IMAGE`.

## 4. Посмотри список и создай профиль

```sh
./awg-profiles --host "$VPS" --user root list
./awg-profiles --host "$VPS" --user root create "iPhone"
```

Клиент сначала пробует SSH-ключ из агента Mac. Если ключ ещё не в агенте, добавь свой ключ командой `ssh-add ~/.ssh/id_ed25519`, подставив путь к своему файлу. Для входа по паролю добавь `--password` перед командой:

```sh
./awg-profiles --host "$VPS" --user root --password list
./awg-profiles --host "$VPS" --user root --password create "iPhone"
```

Выбери один вариант входа. После создания импортируй `iPhone.conf` из папки `~/awg-profile-manager` в AmneziaVPN или AmneziaWG на нужном устройстве. Имя должно быть свободно на VPS и в папке на Mac. Храни `.conf` как пароль.

## 5. Отзови профиль при необходимости

Выбери профиль по имени. Если в `list` он помечен как `(unnamed)`, скопируй его VPN-адрес и используй `--address`.

```sh
./awg-profiles --host "$VPS" --user root revoke --name "iPhone"
./awg-profiles --host "$VPS" --user root revoke --address VPN_АДРЕС_ИЗ_СПИСКА
```

Команда покажет профиль и адрес VPS. Введи `y` для отзыва или нажми Enter для отмены.
