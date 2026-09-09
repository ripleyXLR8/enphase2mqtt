#!/usr/bin/env python3
"""enphase2mqtt — passerelle Enphase IQ Gateway (Envoy) vers MQTT Discovery.

Lit la passerelle Envoy **en local** avec pyenphase (la bibliothèque qui
alimente l'intégration officielle Enphase de Home Assistant) et publie des
messages de découverte au format Home Assistant, consommables tels quels par
Home Assistant ou par le plugin MQTT Discovery de Jeedom.

La passerelle est en lecture seule : elle ne publie aucun topic de commande.
L'Envoy n'expose rien de pilotable sur une installation sans batterie.
"""

from __future__ import annotations

import argparse
import asyncio
import configparser
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import paho.mqtt.client as mqtt
from pyenphase import Envoy
from pyenphase.exceptions import (
    EnvoyAuthenticationError,
    EnvoyAuthenticationRequired,
    EnvoyError,
)

LOGGER = logging.getLogger("enphase2mqtt")

CONFIG_PATH = "/config/enphase2mqtt.conf"
TOKEN_PATH = "/config/enphase_token.json"

APP_VERSION = "1.0"
AUTHOR = "Richard Perez"
GITHUB = "github.com/ripleyXLR8"
EMAIL = "4702185+ripleyXLR8@users.noreply.github.com"

# Un seul couple de charges utiles : le topic de liaison sert aussi de topic
# de disponibilité, voir Bridge._availability_topic.
PAYLOAD_ON = "ON"
PAYLOAD_OFF = "OFF"

# Le jeton d'un compte « owner » vit un an. On le renouvelle bien avant.
TOKEN_RENEW_MARGIN = 30 * 24 * 3600

ENV_OVERRIDES: dict[str, tuple[str, str]] = {
    "ENVOY_HOST": ("envoy", "host"),
    "ENVOY_USER": ("envoy", "username"),
    "ENVOY_PASSWORD": ("envoy", "password"),
    "ENVOY_TOKEN": ("envoy", "token"),
    "ENVOY_TOKEN_FILE": ("envoy", "token_file"),
    "ENVOY_POLL_INTERVAL": ("envoy", "poll_interval"),
    "ENVOY_PANEL_NAMES": ("envoy", "panel_names"),
    "ENVOY_PANEL_GROUPS": ("envoy", "panel_groups"),
    "ENVOY_PUBLISH_PANELS": ("envoy", "publish_panels"),
    "ENVOY_PUBLISH_PHASES": ("envoy", "publish_phases"),
    "MQTT_HOST": ("mqtt", "host"),
    "MQTT_PORT": ("mqtt", "port"),
    "MQTT_USER": ("mqtt", "login"),
    "MQTT_PASSWORD": ("mqtt", "password"),
    "MQTT_CLIENT_ID": ("mqtt", "client_id"),
    "MQTT_DISCOVERY_PREFIX": ("mqtt", "discovery_prefix"),
    "MQTT_TOPIC_PREFIX": ("mqtt", "topic_prefix"),
    "LOG_LEVEL": ("log", "level"),
}


# ----------------------------------------------------------------------
# Mise en forme des valeurs
# ----------------------------------------------------------------------


def as_int(value: Any) -> str | None:
    """Arrondit à l'entier. Les puissances et énergies n'ont pas de décimale utile."""
    if value is None:
        return None
    return str(int(round(float(value))))


def as_dec(digits: int) -> Callable[[Any], str | None]:
    def render(value: Any) -> str | None:
        if value is None:
            return None
        return f"{float(value):.{digits}f}"

    return render


def as_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        # status_flags vaut [] la plupart du temps : « - » se lit mieux qu'un
        # champ vide dans une tuile.
        return ", ".join(str(v) for v in value) if value else "-"
    return str(value)


# ----------------------------------------------------------------------
# Description des entités
#
# Tout est décrit dans des tables : ajouter une grandeur, c'est ajouter une
# ligne. C'est la leçon du plugin GDS3710, où 700 lignes de créations
# recopiées cachaient des divergences que personne ne voyait.
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class Sensor:
    key: str
    name: str
    getter: Callable[..., Any]
    unit: str = ""
    device_class: str = ""
    state_class: str = "measurement"
    render: Callable[[Any], str | None] = as_int
    # Une entité de diagnostic n'a pas vocation à encombrer une tuile.
    diagnostic: bool = False


def _sys(attr: str, field_name: str) -> Callable[[Any], Any]:
    def get(data: Any) -> Any:
        obj = getattr(data, attr, None)
        return getattr(obj, field_name, None) if obj is not None else None

    return get


def _ct(attr: str, field_name: str) -> Callable[[Any], Any]:
    def get(data: Any) -> Any:
        obj = getattr(data, attr, None)
        return getattr(obj, field_name, None) if obj is not None else None

    return get


ENERGY_TOTAL = "total_increasing"

# --- Passerelle : 24 entités -----------------------------------------------

GATEWAY_SENSORS: tuple[Sensor, ...] = (
    # Production
    Sensor("prod_w", "Production", _sys("system_production", "watts_now"), "W", "power"),
    Sensor("prod_wh_today", "Production du jour", _sys("system_production", "watt_hours_today"), "Wh", "energy", ENERGY_TOTAL),
    Sensor("prod_wh_7d", "Production 7 jours", _sys("system_production", "watt_hours_last_7_days"), "Wh", "energy", ENERGY_TOTAL),
    Sensor("prod_wh_total", "Production totale", _sys("system_production", "watt_hours_lifetime"), "Wh", "energy", ENERGY_TOTAL),
    # Consommation totale du logement
    Sensor("conso_w", "Consommation", _sys("system_consumption", "watts_now"), "W", "power"),
    Sensor("conso_wh_today", "Consommation du jour", _sys("system_consumption", "watt_hours_today"), "Wh", "energy", ENERGY_TOTAL),
    Sensor("conso_wh_7d", "Consommation 7 jours", _sys("system_consumption", "watt_hours_last_7_days"), "Wh", "energy", ENERGY_TOTAL),
    Sensor("conso_wh_total", "Consommation totale", _sys("system_consumption", "watt_hours_lifetime"), "Wh", "energy", ENERGY_TOTAL),
    # Consommation nette (échange avec le réseau).
    # « du jour » et « 7 jours » valent 0 sur cette grandeur : l'Envoy ne les
    # calcule pas. Les publier créerait deux commandes définitivement vides.
    Sensor("net_w", "Consommation nette", _sys("system_net_consumption", "watts_now"), "W", "power"),
    Sensor("net_wh_total", "Consommation nette totale", _sys("system_net_consumption", "watt_hours_lifetime"), "Wh", "energy", ENERGY_TOTAL),
    # Compteur de production
    Sensor("ct_prod_pf", "Facteur de puissance production", _ct("ctmeter_production", "power_factor"), "", "power_factor", render=as_dec(3)),
    Sensor("ct_prod_a", "Courant production", _ct("ctmeter_production", "current"), "A", "current", render=as_dec(3)),
    # Tension et fréquence ne sont publiées qu'une fois : les deux compteurs
    # mesurent le même réseau (240,799 V contre 240,803 V au relevé).
    Sensor("grid_v", "Tension réseau", _ct("ctmeter_production", "voltage"), "V", "voltage", render=as_dec(1)),
    Sensor("grid_hz", "Fréquence réseau", _ct("ctmeter_production", "frequency"), "Hz", "frequency", render=as_dec(2)),
    Sensor("ct_prod_wh_received", "Consommation des onduleurs", _ct("ctmeter_production", "energy_received"), "Wh", "energy", ENERGY_TOTAL, diagnostic=True),
    Sensor("ct_prod_status", "État compteur production", _ct("ctmeter_production", "metering_status"), render=as_text, state_class="", diagnostic=True),
    Sensor("ct_prod_flags", "Anomalies compteur production", _ct("ctmeter_production", "status_flags"), render=as_text, state_class="", diagnostic=True),
    # Compteur réseau
    Sensor("ct_net_pf", "Facteur de puissance réseau", _ct("ctmeter_consumption", "power_factor"), "", "power_factor", render=as_dec(3)),
    Sensor("ct_net_a", "Courant réseau", _ct("ctmeter_consumption", "current"), "A", "current", render=as_dec(3)),
    Sensor("ct_net_wh_delivered", "Énergie soutirée", _ct("ctmeter_consumption", "energy_delivered"), "Wh", "energy", ENERGY_TOTAL),
    Sensor("ct_net_wh_received", "Énergie injectée", _ct("ctmeter_consumption", "energy_received"), "Wh", "energy", ENERGY_TOTAL),
    Sensor("ct_net_status", "État compteur réseau", _ct("ctmeter_consumption", "metering_status"), render=as_text, state_class="", diagnostic=True),
    Sensor("ct_net_flags", "Anomalies compteur réseau", _ct("ctmeter_consumption", "status_flags"), render=as_text, state_class="", diagnostic=True),
)

# --- Par phase : 7 entités × 3 phases = 21 ---------------------------------


def _phase(attr: str, field_name: str) -> Callable[[Any, str], Any]:
    def get(data: Any, phase: str) -> Any:
        table = getattr(data, attr, None) or {}
        obj = table.get(phase)
        return getattr(obj, field_name, None) if obj is not None else None

    return get


PHASE_SENSORS: tuple[Sensor, ...] = (
    Sensor("prod_w", "Production {ph}", _phase("system_production_phases", "watts_now"), "W", "power"),
    Sensor("prod_wh_today", "Production du jour {ph}", _phase("system_production_phases", "watt_hours_today"), "Wh", "energy", ENERGY_TOTAL),
    Sensor("conso_w", "Consommation {ph}", _phase("system_consumption_phases", "watts_now"), "W", "power"),
    Sensor("conso_wh_today", "Consommation du jour {ph}", _phase("system_consumption_phases", "watt_hours_today"), "Wh", "energy", ENERGY_TOTAL),
    Sensor("net_w", "Consommation nette {ph}", _phase("system_net_consumption_phases", "watts_now"), "W", "power"),
    Sensor("grid_v", "Tension {ph}", _phase("ctmeter_production_phases", "voltage"), "V", "voltage", render=as_dec(1)),
    Sensor("pf", "Facteur de puissance {ph}", _phase("ctmeter_production_phases", "power_factor"), "", "power_factor", render=as_dec(3)),
)

# --- Par panneau : 6 entités × 10 = 60 -------------------------------------


def _inv(field_name: str) -> Callable[[Any], Any]:
    def get(inverter: Any) -> Any:
        return getattr(inverter, field_name, None)

    return get


PANEL_SENSORS: tuple[Sensor, ...] = (
    Sensor("w", "Puissance", _inv("last_report_watts"), "W", "power"),
    Sensor("wh_today", "Production du jour", _inv("energy_today"), "Wh", "energy", ENERGY_TOTAL),
    Sensor("wh_total", "Production totale", _inv("lifetime_energy"), "Wh", "energy", ENERGY_TOTAL),
    Sensor("dc_v", "Tension DC", _inv("dc_voltage"), "V", "voltage", render=as_dec(1)),
    Sensor("dc_a", "Courant DC", _inv("dc_current"), "A", "current", render=as_dec(3)),
    Sensor("temp", "Température", _inv("temperature"), "°C", "temperature", render=as_dec(0)),
)


# --- Par champ : 4 entités par groupe de panneaux -------------------------


def _grp(field_name: str) -> Callable[[Any], Any]:
    """Somme une grandeur sur les panneaux d'un champ.

    Un panneau qui n'a pas encore remonté la grandeur est ignoré plutôt que
    compté pour zéro : mieux vaut une somme partielle qu'un creux inventé.
    """

    def get(inverters: Any) -> Any:
        values = [
            getattr(inv, field_name, None)
            for inv in inverters
            if getattr(inv, field_name, None) is not None
        ]
        return sum(values) if values else None

    return get


GROUP_SENSORS: tuple[Sensor, ...] = (
    Sensor("w", "Puissance", _grp("last_report_watts"), "W", "power"),
    Sensor("wh_today", "Production du jour", _grp("energy_today"), "Wh", "energy", ENERGY_TOTAL),
    Sensor("wh_total", "Production totale", _grp("lifetime_energy"), "Wh", "energy", ENERGY_TOTAL),
    # Puissance crête installée du champ : constante, donc diagnostic.
    Sensor("w_max", "Puissance max", _grp("max_report_watts"), "W", "power", state_class="", diagnostic=True),
)


def slugify(value: str) -> str:
    out = []
    for char in value.lower():
        out.append(char if char.isalnum() else "_")
    slug = "".join(out).strip("_")
    while "__" in slug:
        slug = slug.replace("__", "_")
    return slug or "groupe"


# ----------------------------------------------------------------------
# Modèle
# ----------------------------------------------------------------------


@dataclass
class Device:
    """Un appareil au sens MQTT Discovery : la passerelle, ou un panneau."""

    device_id: str
    name: str
    model: str
    base_topic: str
    sensors: tuple[Sensor, ...]
    # Extrait de EnvoyData l'objet que les getters de ce device attendent.
    source: Callable[[Any], Any]
    via: str = ""
    phase: str = ""

    def state_topic(self, sensor: Sensor) -> str:
        return f"{self.base_topic}/{sensor.key}/state"


class Bridge:
    def __init__(self, config: configparser.ConfigParser) -> None:
        self._config = config
        self._prefix = config.get("mqtt", "topic_prefix", fallback="enphase").strip("/")
        self._discovery_prefix = config.get(
            "mqtt", "discovery_prefix", fallback="homeassistant"
        ).strip("/")
        self._token_file = config.get("envoy", "token_file", fallback=TOKEN_PATH)
        self._poll = max(15, config.getint("envoy", "poll_interval", fallback=60))
        self._publish_panels = config.getboolean("envoy", "publish_panels", fallback=True)
        self._publish_phases = config.getboolean("envoy", "publish_phases", fallback=True)
        self._panel_names = self._parse_panel_names(
            config.get("envoy", "panel_names", fallback="")
        )
        self._panel_groups = self._parse_panel_groups(
            config.get("envoy", "panel_groups", fallback="")
        )

        self._envoy: Envoy | None = None
        self._mqtt: mqtt.Client | None = None
        self._devices: dict[str, Device] = {}
        self._serial = ""
        self._online = False
        self._data: Any = None
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_panel_names(raw: str) -> dict[str, str]:
        """« 482233004696:Panneau 1, 482233004841:Panneau 2 » -> dict."""
        names: dict[str, str] = {}
        for chunk in raw.split(","):
            chunk = chunk.strip()
            if not chunk or ":" not in chunk:
                continue
            serial, _, label = chunk.partition(":")
            serial, label = serial.strip(), label.strip()
            if serial and label:
                names[serial] = label
        return names

    @staticmethod
    def _parse_panel_groups(raw: str) -> list[tuple[str, list[str]]]:
        """« Champ 1:sn1+sn2, Champ 2:sn3 » -> [(nom, [sn...]), ...].

        L'ordre de déclaration est conservé : c'est celui de l'affichage.
        """
        groups: list[tuple[str, list[str]]] = []
        for chunk in raw.split(","):
            chunk = chunk.strip()
            if not chunk or ":" not in chunk:
                continue
            name, _, members = chunk.partition(":")
            name = name.strip()
            serials = [m.strip() for m in members.split("+") if m.strip()]
            if name and serials:
                groups.append((name, serials))
        return groups

    @property
    def _availability_topic(self) -> str:
        """Disponibilité et témoin de liaison partagent **un seul** topic.

        MQTT n'autorise qu'un testament par connexion. Avec deux topics, une
        mort brutale n'en corrige qu'un : l'autre reste bloqué sur « en ligne »
        et ment jusqu'au prochain démarrage. Un topic unique, porté par le
        testament, ne peut pas diverger.
        """
        return f"{self._prefix}/{self._serial}/link/state"

    # ------------------------------------------------------------------
    # Jeton
    # ------------------------------------------------------------------

    def _load_token(self) -> str:
        token = self._config.get("envoy", "token", fallback="").strip()
        try:
            with open(self._token_file, encoding="utf-8") as fh:
                stored = json.load(fh).get("token", "").strip()
            if stored:
                return stored
        except (OSError, ValueError):
            pass
        return token

    def _save_token(self, token: str) -> None:
        if not token:
            return
        try:
            path = self._token_file
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"token": token, "saved_at": int(time.time())}, fh)
            # Le jeton vaut un accès complet à la passerelle : personne d'autre
            # n'a à le lire.
            os.chmod(path, 0o600)
            LOGGER.info("Jeton enregistré dans %s", path)
        except OSError as err:
            LOGGER.warning("Impossible d'enregistrer le jeton : %s", err)

    async def _authenticate(self, envoy: Envoy) -> None:
        user = self._config.get("envoy", "username", fallback="").strip()
        password = self._config.get("envoy", "password", fallback="")
        token = self._load_token()
        try:
            await envoy.authenticate(username=user, password=password, token=token or None)
        except EnvoyAuthenticationError:
            # Jeton périmé ou refusé : on en redemande un, sans le passer.
            LOGGER.warning("Jeton refusé, demande d'un nouveau jeton à Enlighten")
            await envoy.authenticate(username=user, password=password)
        self._store_current_token(envoy)

    def _store_current_token(self, envoy: Envoy) -> None:
        token = getattr(getattr(envoy, "auth", None), "token", "")
        if token and token != self._load_token():
            self._save_token(token)

    async def _maybe_renew_token(self, envoy: Envoy) -> None:
        """Renouvelle le jeton bien avant son expiration.

        La documentation amont propose ici `expire < now - 7 jours`, ce qui
        n'est vrai qu'une semaine *après* la péremption — donc jamais à temps.
        La comparaison correcte est avec un instant futur.
        """
        auth = getattr(envoy, "auth", None)
        expiry = getattr(auth, "expire_timestamp", None)
        if auth is None or expiry is None:
            return
        expiry = getattr(expiry, "timestamp", lambda: expiry)()
        if float(expiry) > time.time() + TOKEN_RENEW_MARGIN:
            return
        LOGGER.info("Jeton proche de l'expiration, renouvellement")
        try:
            await auth.refresh()
        except EnvoyError as err:
            LOGGER.warning("Renouvellement du jeton impossible : %s", err)
            return
        self._store_current_token(envoy)

    # ------------------------------------------------------------------
    # Construction des appareils
    # ------------------------------------------------------------------

    def _build_devices(self, envoy: Envoy, data: Any) -> None:
        self._serial = str(getattr(envoy, "serial_number", "") or "envoy")
        gateway_id = f"enphase_{self._serial}"
        firmware = str(getattr(envoy, "firmware", "") or "")

        self._devices["gateway"] = Device(
            device_id=gateway_id,
            name="Envoy",
            model=f"IQ Gateway {firmware}".strip(),
            base_topic=f"{self._prefix}/{self._serial}",
            sensors=GATEWAY_SENSORS,
            source=lambda d: d,
        )

        if self._publish_phases:
            phases = sorted((getattr(data, "system_production_phases", None) or {}).keys())
            for phase in phases:
                key = f"phase_{phase.lower()}"
                self._devices[key] = Device(
                    device_id=f"{gateway_id}_{key}",
                    name=f"Envoy phase {phase}",
                    model="Phase",
                    base_topic=f"{self._prefix}/{self._serial}/{key}",
                    sensors=PHASE_SENSORS,
                    source=lambda d: d,
                    via=gateway_id,
                    phase=phase,
                )
            LOGGER.info("%d phase(s) détectée(s) : %s", len(phases), ", ".join(phases))

        if self._publish_panels:
            inverters = getattr(data, "inverters", None) or {}
            for index, serial in enumerate(sorted(inverters), start=1):
                serial = str(serial)
                label = self._panel_names.get(serial, f"Panneau {index}")
                key = f"panel_{serial}"
                self._devices[key] = Device(
                    device_id=f"{gateway_id}_{key}",
                    name=label,
                    model="Micro-onduleur IQ",
                    base_topic=f"{self._prefix}/{self._serial}/panneau/{serial}",
                    sensors=PANEL_SENSORS,
                    source=(lambda s: lambda d: (getattr(d, "inverters", None) or {}).get(s))(serial),
                    via=gateway_id,
                )
            LOGGER.info("%d micro-onduleur(s) détecté(s)", len(inverters))
            self._build_groups(gateway_id, inverters)

        total = sum(len(dev.sensors) for dev in self._devices.values()) + 1
        LOGGER.info(
            "%d appareil(s), %d entité(s) publiée(s)", len(self._devices), total
        )

    def _build_groups(self, gateway_id: str, inverters: dict[str, Any]) -> None:
        """Un appareil par champ : les grandeurs des panneaux, sommées.

        Un champ est un sous-ensemble de panneaux partageant une orientation ou
        une chaîne. Rien ne l'impose : sans configuration, aucun champ.
        """
        known = {str(serial) for serial in inverters}
        groupes = 0
        for name, serials in self._panel_groups:
            absents = [sn for sn in serials if sn not in known]
            if absents:
                # Une somme silencieusement amputée serait pire qu'une erreur.
                LOGGER.warning(
                    "Champ « %s » : %d numéro(s) de série inconnu(s) de la "
                    "passerelle, ignoré(s) : %s",
                    name,
                    len(absents),
                    ", ".join(absents),
                )
            membres = [sn for sn in serials if sn in known]
            if not membres:
                LOGGER.error("Champ « %s » : aucun panneau connu, champ ignoré", name)
                continue
            key = f"group_{slugify(name)}"
            if key in self._devices:
                LOGGER.error("Champ « %s » : nom en double, champ ignoré", name)
                continue
            self._devices[key] = Device(
                device_id=f"{gateway_id}_{key}",
                name=name,
                model=f"Champ de {len(membres)} panneaux",
                base_topic=f"{self._prefix}/{self._serial}/champ/{slugify(name)}",
                sensors=GROUP_SENSORS,
                source=(
                    lambda m: lambda d: [
                        inv
                        for sn, inv in (getattr(d, "inverters", None) or {}).items()
                        if str(sn) in m
                    ]
                )(set(membres)),
                via=gateway_id,
            )
            groupes += 1
        if groupes:
            LOGGER.info("%d champ(s) de panneaux construit(s)", groupes)

    # ------------------------------------------------------------------
    # Découverte
    # ------------------------------------------------------------------

    def _device_block(self, device: Device) -> dict[str, Any]:
        block: dict[str, Any] = {
            "identifiers": [device.device_id],
            "name": device.name,
            "manufacturer": "Enphase",
        }
        if device.model:
            block["model"] = device.model
        if device.via:
            block["via_device"] = device.via
        return block

    def _discovery_topic(self, device: Device, key: str, component: str) -> str:
        return f"{self._discovery_prefix}/{component}/{device.device_id}/{key}/config"

    def _publish_discovery(self) -> None:
        for device in self._devices.values():
            for sensor in device.sensors:
                payload: dict[str, Any] = {
                    "name": sensor.name.format(ph=device.phase),
                    "unique_id": f"{device.device_id}_{sensor.key}",
                    "object_id": f"{device.device_id}_{sensor.key}",
                    "state_topic": device.state_topic(sensor),
                    "device": self._device_block(device),
                    "availability_topic": self._availability_topic,
                    "payload_available": PAYLOAD_ON,
                    "payload_not_available": PAYLOAD_OFF,
                }
                if sensor.unit:
                    payload["unit_of_measurement"] = sensor.unit
                if sensor.device_class:
                    payload["device_class"] = sensor.device_class
                if sensor.state_class:
                    payload["state_class"] = sensor.state_class
                if sensor.diagnostic:
                    payload["entity_category"] = "diagnostic"
                self._publish(
                    self._discovery_topic(device, sensor.key, "sensor"),
                    json.dumps(payload, ensure_ascii=False),
                )

        # Témoin de liaison : seule entité qui doit rester lisible quand le
        # lien est coupé, donc sans topic de disponibilité.
        gateway = self._devices["gateway"]
        payload = {
            "name": "En ligne",
            "unique_id": f"{gateway.device_id}_link",
            "object_id": f"{gateway.device_id}_link",
            "state_topic": self._availability_topic,
            "device_class": "connectivity",
            "payload_on": PAYLOAD_ON,
            "payload_off": PAYLOAD_OFF,
            "device": self._device_block(gateway),
        }
        self._publish(
            self._discovery_topic(gateway, "link", "binary_sensor"),
            json.dumps(payload, ensure_ascii=False),
        )

    # ------------------------------------------------------------------
    # MQTT
    # ------------------------------------------------------------------

    def _publish(self, topic: str, payload: str, retain: bool = True) -> None:
        if self._mqtt is None:
            return
        LOGGER.debug("MQTT -> %s = %s", topic, payload)
        self._mqtt.publish(topic, payload, qos=1, retain=retain)

    def _publish_states(self) -> None:
        if self._data is None:
            return
        for device in self._devices.values():
            source = device.source(self._data)
            if source is None:
                continue
            for sensor in device.sensors:
                try:
                    raw = (
                        sensor.getter(source, device.phase)
                        if device.phase
                        else sensor.getter(source)
                    )
                    value = sensor.render(raw)
                except (TypeError, ValueError) as err:
                    LOGGER.debug("Valeur illisible pour %s : %s", sensor.key, err)
                    continue
                if value is not None:
                    self._publish(device.state_topic(sensor), value)

    def _publish_availability(self) -> None:
        self._publish(
            self._availability_topic, PAYLOAD_ON if self._online else PAYLOAD_OFF
        )

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        """Rejoue découverte et états à *chaque* connexion.

        En session non persistante le broker oublie tout à chaque coupure.
        C'est la panne qui a immobilisé le pont Velux pendant des semaines :
        il publiait toujours, mais plus rien ne lui parvenait.
        """
        if reason_code != 0:
            LOGGER.error("Connexion MQTT refusée : %s", reason_code)
            return
        LOGGER.info("Connecté au broker MQTT, (re)publication de la découverte")
        if self._devices:
            self._publish_discovery()
            self._publish_availability()
            self._publish_states()

    def _on_disconnect(self, client, userdata, *args):
        LOGGER.warning("Déconnexion du broker MQTT, reconnexion automatique")

    def _connect_mqtt(self) -> mqtt.Client:
        cfg = self._config
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=cfg.get("mqtt", "client_id", fallback="enphase2mqtt"),
        )
        user = cfg.get("mqtt", "login", fallback="").strip()
        if user:
            client.username_pw_set(user, cfg.get("mqtt", "password", fallback=""))
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        # Testament : si la passerelle meurt, « En ligne » retombe tout seul.
        client.will_set(self._availability_topic, PAYLOAD_OFF, qos=1, retain=True)
        client.connect_async(
            cfg.get("mqtt", "host", fallback="127.0.0.1"),
            cfg.getint("mqtt", "port", fallback=1883),
            keepalive=60,
        )
        client.loop_start()
        return client

    # ------------------------------------------------------------------
    # Boucle
    # ------------------------------------------------------------------

    async def run(self, dump_only: bool = False) -> None:
        host = self._config.get("envoy", "host", fallback="").strip()
        if not host:
            raise SystemExit("ENVOY_HOST n'est pas renseigné")

        envoy = Envoy(host)
        self._envoy = envoy
        await envoy.setup()
        LOGGER.info(
            "Envoy %s, firmware %s",
            getattr(envoy, "serial_number", "?"),
            getattr(envoy, "firmware", "?"),
        )
        await self._authenticate(envoy)

        data = await envoy.update()
        self._data = data

        if dump_only:
            await envoy.close()
            self._dump(envoy, data)
            return

        self._build_devices(envoy, data)
        self._serial = self._serial or "envoy"
        self._mqtt = self._connect_mqtt()
        self._online = True

        try:
            while not self._stop.is_set():
                self._publish_availability()
                self._publish_states()
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self._poll)
                except asyncio.TimeoutError:
                    pass
                if self._stop.is_set():
                    break
                await self._refresh(envoy)
        finally:
            await self._shutdown(envoy)

    async def _refresh(self, envoy: Envoy) -> None:
        try:
            await self._maybe_renew_token(envoy)
            self._data = await envoy.update()
            if not self._online:
                LOGGER.info("Passerelle de nouveau joignable")
            self._online = True
        except EnvoyAuthenticationRequired:
            LOGGER.warning("Authentification expirée, nouvelle authentification")
            try:
                await self._authenticate(envoy)
            except EnvoyError as err:
                LOGGER.error("Ré-authentification impossible : %s", err)
                self._online = False
        except EnvoyError as err:
            # Une coupure réseau ou un redémarrage de l'Envoy ne doit pas tuer
            # le conteneur : on repasse « hors ligne » et on retente.
            if self._online:
                LOGGER.warning("Lecture de la passerelle impossible : %s", err)
            self._online = False

    async def _shutdown(self, envoy: Envoy) -> None:
        LOGGER.info("Arrêt")
        self._online = False
        self._publish_availability()
        if self._mqtt is not None:
            self._mqtt.loop_stop()
            self._mqtt.disconnect()
        try:
            await envoy.close()
        except Exception as err:  # noqa: BLE001 - l'arrêt ne doit jamais échouer
            LOGGER.debug("Fermeture de la session Envoy : %s", err)

    def _dump(self, envoy: Envoy, data: Any) -> None:
        """Inventaire lisible, sans broker ni publication."""
        out: dict[str, Any] = {
            "serial_number": getattr(envoy, "serial_number", None),
            "firmware": getattr(envoy, "firmware", None),
            "part_number": getattr(envoy, "part_number", None),
            "phase_count": getattr(envoy, "phase_count", None),
            "ct_meter_list": [str(c) for c in getattr(envoy, "ct_meter_list", []) or []],
            "inverters": sorted(str(s) for s in (getattr(data, "inverters", None) or {})),
        }
        print(json.dumps(out, indent=2, ensure_ascii=False))

    def request_stop(self) -> None:
        LOGGER.info("Signal reçu, arrêt en cours")
        self._stop.set()


# ----------------------------------------------------------------------
# Démarrage
# ----------------------------------------------------------------------


def load_config(path: str) -> configparser.ConfigParser:
    """Charge le fichier de configuration, puis applique l'environnement.

    Le fichier est facultatif : une configuration entièrement fournie par
    variables d'environnement est valide.
    """
    config = configparser.ConfigParser()
    config.read(path, encoding="utf-8")
    for env_name, (section, option) in ENV_OVERRIDES.items():
        value = os.environ.get(env_name)
        if value is None or value == "":
            continue
        if not config.has_section(section):
            config.add_section(section)
        config.set(section, option, value)
    for section in ("envoy", "mqtt", "log"):
        if not config.has_section(section):
            config.add_section(section)
    return config


def print_banner(config: configparser.ConfigParser) -> None:
    def g(section: str, opt: str, default: str = "") -> str:
        return config.get(section, opt, fallback=default)

    lines = [
        f"Version    : {APP_VERSION}",
        f"MQTT       : {g('mqtt', 'host', '127.0.0.1')}:{g('mqtt', 'port', '1883')}"
        f"  (discovery={g('mqtt', 'discovery_prefix', 'homeassistant')},"
        f" topics={g('mqtt', 'topic_prefix', 'enphase')})",
        f"Envoy      : {g('envoy', 'host', '-')} en local"
        f" — relevé toutes les {g('envoy', 'poll_interval', '60')}s",
        "",
        f"Auteur     : {AUTHOR}",
        f"GitHub     : {GITHUB}",
        f"Email      : {EMAIL}",
    ]
    art = [
        r"                 _                 ___              _   _   ",
        r"  ___ _ _  _ __ | |_  __ _ ___ ___|_  )_ __  __ _ _| |_| |_ ",
        r" / -_) ' \| '_ \| ' \/ _` (_-</ -_)/ /| '  \/ _` |  _|  _|  ",
        r" \___|_||_| .__/|_||_\__,_/__/\___/___|_|_|_\__, |\__|\__|  ",
        r"          |_|   Enphase IQ Gateway -> MQTT   |_|            ",
    ]
    width = max(max(len(a) for a in art), max(len(l) for l in lines)) + 2
    out = ["", "+" + "-" * width + "+"]
    for a in art:
        out.append("|" + a.ljust(width) + "|")
    out.append("+" + "-" * width + "+")
    for l in lines:
        out.append("| " + l.ljust(width - 1) + "|")
    out.append("+" + "-" * width + "+")
    out.append("")
    print("\n".join(out), flush=True)


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Passerelle Enphase IQ Gateway (Envoy) vers MQTT Discovery"
    )
    parser.add_argument("config", nargs="?", default=CONFIG_PATH)
    parser.add_argument(
        "--dump",
        action="store_true",
        help="affiche l'inventaire de la passerelle en JSON, puis quitte",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    logging.basicConfig(
        level=config.get("log", "level", fallback="INFO").upper(),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        stream=sys.stderr if args.dump else sys.stdout,
    )
    if not args.dump:
        print_banner(config)

    bridge = Bridge(config)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, bridge.request_stop)
        except NotImplementedError:
            # Windows n'a pas add_signal_handler. Le conteneur tourne sous
            # Linux, mais --dump doit rester utilisable partout.
            signal.signal(sig, lambda *_: bridge.request_stop())

    try:
        await bridge.run(dump_only=args.dump)
    except EnvoyAuthenticationError as err:
        LOGGER.error(
            "Identifiants Enlighten refusés (%s) — vérifier ENVOY_USER et "
            "ENVOY_PASSWORD, et supprimer le fichier de jeton si besoin",
            err,
        )
        return 2
    except EnvoyError as err:
        LOGGER.error("Erreur de la passerelle Envoy : %s", err)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
