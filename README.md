# AmneziaWG Profile Manager

Создавай, просматривай и отзывай профили AmneziaWG на своём VPS с Mac. На VPS один раз запускается менеджер. На Mac скрипт `awg-profiles` запускает клиентский контейнер для каждой команды и обращается к VPS по SSH.

## 1. Настрой AmneziaWG на VPS

В AmneziaVPN на Mac выбери **Self-hosted VPN**, введи IP своего VPS и данные для входа по SSH, затем установи AmneziaWG. После установки нажми «Подключиться» и проверь, что VPN работает. [Инструкция Amnezia](https://docs.amnezia.org/documentation/instructions/install-vpn-on-server/)

## 2. Запусти менеджер на VPS

В терминале **на Mac** замени `203.0.113.10` на публичный IP своего VPS. В примерах используется SSH-пользователь `root`. Если у тебя другой пользователь, подставь его в команды. На VPS у него должен быть доступ к Docker.

```sh
VPS=203.0.113.10
ssh "root@${VPS}"
```

При первом входе сверь отпечаток ключа VPS с данными в консоли провайдера и подтверди его.

**На VPS** проверь имя контейнера с VPN:

```sh
docker ps --format '{{.Names}}'
```

Подставь это имя в первую строку следующего блока. Для установки через AmneziaVPN это обычно `amnezia-awg2`. Если в списке указан `amnezia-awg`, используй его.

```sh
AWG_CONTAINER=amnezia-awg2
IMAGE=dvononintech/awg-profile-server:1.0.1

docker pull "$IMAGE" &&
docker run -d --restart unless-stopped --name awg-profile-manager \
  -e "AWG_CONTAINER=$AWG_CONTAINER" \
  --mount type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock \
  "$IMAGE"

docker ps --filter name=awg-profile-manager
```

Когда `awg-profile-manager` покажет `Up`, введи `exit`, чтобы вернуться на Mac.

## 3. Скачай клиент

Открой Docker Desktop и выполни в терминале:

```sh
git clone https://github.com/dvoronin-tech/manage-profiles-awg.git
cd manage-profiles-awg
export AWG_CLIENT_IMAGE=dvononintech/awg-profile-client:1.0.0
docker pull "$AWG_CLIENT_IMAGE"
```

## 4. Проверь список и создай профиль

В том же терминале **на Mac**:

```sh
./awg-profiles --host "$VPS" --user root list
./awg-profiles --host "$VPS" --user root create "phone"
```

В списке появится новый профиль с VPN-адресом. Файл `phone.conf` сохранится рядом с `awg-profiles`. Импортируй его в AmneziaVPN или AmneziaWG на нужном устройстве, подключись и открой любой сайт. Храни файл как пароль.

Клиент сначала пробует ключи из SSH-агента Mac. Чтобы разрешить вход по паролю, добавь `--password` перед `list`, `create` или `revoke`. В новом терминале снова задай `VPS` и `AWG_CLIENT_IMAGE`.

## Отзыв профиля

```sh
./awg-profiles --host "$VPS" --user root revoke --name "phone"
```

Если профиль в списке помечен как `(unnamed)`, используй `revoke --address VPN_АДРЕС_ИЗ_СПИСКА`. Проверь профиль и адрес VPS в приглашении, затем введи `y` для подтверждения.

## Если конфиг VPN смонтирован только для чтения

На VPS выполни команду ниже. В первом столбце будет путь к файлу на VPS, во втором — путь внутри VPN-контейнера, в третьем — флаг записи.

```sh
docker inspect --format '{{range .Mounts}}{{println .Source .Destination .RW}}{{end}}' "$AWG_CONTAINER"
```

Если в строке с `awg0.conf` стоит `false`, подставь путь из первого столбца в `CONF` и замени только менеджер. Блок использует переменные `AWG_CONTAINER` и `IMAGE` из шага 2.

```sh
CONF=/путь/на/VPS/к/awg0.conf
test -f "$CONF" &&
docker pull "$IMAGE" &&
docker rm -f awg-profile-manager &&
docker run -d --restart unless-stopped --name awg-profile-manager \
  -e "AWG_CONTAINER=$AWG_CONTAINER" \
  -e AWG_WRITABLE_CONFIG_PATH=/awg0.conf \
  --mount type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock \
  --mount "type=bind,source=$CONF,target=/awg0.conf" \
  "$IMAGE"
```

VPN-контейнер продолжит работать. После обновления менеджера повтори `create` на Mac.
