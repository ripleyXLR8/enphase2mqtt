#!/usr/bin/env python3
"""Vérifie la construction des entités MQTT Discovery, hors réseau.

Ne demande ni broker ni passerelle : le jeu d'essai reproduit la forme réelle
d'un EnvoyData triphasé à 10 micro-onduleurs, relevée sur une installation.

    python tests/test_mapping.py
"""

from __future__ import annotations

import configparser
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import enphase2mqtt as e2m  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "envoy_three_phase.json")

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"  ok   {message}")
    else:
        failures.append(message)
        print(f"  FAIL {message}")


# ----------------------------------------------------------------------
# Doublures : des objets à attributs, comme ceux de pyenphase.
# ----------------------------------------------------------------------


class Obj:
    def __init__(self, mapping):
        for key, value in mapping.items():
            setattr(self, key, value)


def inflate(node):
    if isinstance(node, dict):
        return Obj({k: inflate(v) for k, v in node.items()})
    if isinstance(node, list):
        return [inflate(v) for v in node]
    return node


def build(**options):
    """Construit un pont et ses appareils, sans aucune connexion."""
    raw = json.load(open(FIXTURE, encoding="utf-8"))

    data = Obj({})
    for key, value in raw["data"].items():
        if key in ("inverters", "system_production_phases",
                   "system_consumption_phases", "system_net_consumption_phases",
                   "ctmeter_production_phases", "ctmeter_consumption_phases"):
            setattr(data, key, {k: inflate(v) for k, v in value.items()})
        else:
            setattr(data, key, inflate(value))

    envoy = Obj(raw["envoy"])

    config = configparser.ConfigParser()
    for section in ("envoy", "mqtt", "log"):
        config.add_section(section)
    config.set("mqtt", "topic_prefix", "enphase")
    config.set("mqtt", "discovery_prefix", "homeassistant")
    for key, value in options.items():
        config.set("envoy", key, value)

    bridge = e2m.Bridge(config)
    bridge._build_devices(envoy, data)
    return bridge, data


print("== construction des appareils ==")
bridge, data = build()
devices = bridge._devices

check("gateway" in devices, "la passerelle est un appareil à part entière")
check(len([k for k in devices if k.startswith("phase_")]) == 3,
      "les 3 phases donnent 3 appareils")
check(len([k for k in devices if k.startswith("panel_")]) == 10,
      "les 10 micro-onduleurs donnent 10 appareils")

total = sum(len(d.sensors) for d in devices.values()) + 1
check(total == 144,
      f"la curation tient ses 144 entités (obtenu : {total})")
check(len(devices["gateway"].sensors) == 25,
      "25 capteurs sur la passerelle, plus le témoin de liaison = 26 entités")
check(len(e2m.PHASE_SENSORS) == 16, "16 entités par phase")
check(len(e2m.PANEL_SENSORS) == 7, "7 entités par panneau")

print("\n== unicité ==")
unique_ids, names_per_device, topics = [], {}, []
for device in devices.values():
    labels = []
    for sensor in device.sensors:
        unique_ids.append(f"{device.device_id}_{sensor.key}")
        labels.append(sensor.name.format(ph=device.phase))
        topics.append(device.state_topic(sensor))
    names_per_device[device.device_id] = labels

check(len(unique_ids) == len(set(unique_ids)), "aucun unique_id en double")
check(len(topics) == len(set(topics)), "aucun topic d'état en double")
dupes = {d: n for d, n in names_per_device.items() if len(n) != len(set(n))}
# Jeedom fusionne silencieusement deux commandes de même nom sur un même
# équipement, et en supprime une. Un doublon ici coûterait une commande.
check(not dupes, f"aucun nom en double dans un même appareil ({dupes})")

print("\n== pièges Jeedom ==")
all_names = [n for names in names_per_device.values() for n in names] + ["En ligne"]
apostrophes = [n for n in all_names if "'" in n]
# Le cœur de Jeedom supprime l'apostrophe ASCII à l'écriture, sur n'importe
# quelle commande : un nom qui en contient arrive tronqué et illisible.
check(not apostrophes, f"aucune apostrophe ASCII dans un nom ({apostrophes})")

# Les libelles francais s ecrivent avec leurs accents. La derive est facile
# quand on ajoute une ligne de table sans relire ses voisines.
SANS_ACCENT = ("Energie", "Etat", "Temperature", "Frequence", "Reseau", "Cle")
fautes = [n for n in all_names for m in SANS_ACCENT if m in n]
check(not fautes, f"aucun libelle francais desaccentue ({fautes})")

vides = [
    s.key for s in e2m.GATEWAY_SENSORS
    if s.getter(data) is None
]
# Le plugin remplacé crée 214 commandes dont 91 restent vides à jamais.
check(not vides, f"aucune entité passerelle sans valeur ({vides})")

net = [s.key for s in e2m.GATEWAY_SENSORS if s.key.startswith("net_")]
check("net_wh_today" not in net and "net_wh_7d" not in net,
      "les agrégats de consommation nette, toujours à 0, ne sont pas publiés")

grid = [s.key for s in e2m.GATEWAY_SENSORS if s.key in ("grid_v", "grid_hz")]
check(len(grid) == 2 and not any(
    s.key.startswith("ct_net") and s.device_class in ("voltage", "frequency")
    for s in e2m.GATEWAY_SENSORS),
    "tension et fréquence ne sont publiées qu'une fois, pas par compteur")

print("\n== rendu des valeurs ==")
check(e2m.as_int(1963.4) == "1963", "les puissances sont arrondies à l'entier")
check(e2m.as_dec(3)(0.9271) == "0.927", "le facteur de puissance garde 3 décimales")
check(e2m.as_text([]) == "-", "une liste d'anomalies vide se lit « - »")
check(e2m.as_text(["ct-fault"]) == "ct-fault", "les anomalies sont listées")
check(e2m.as_int(None) is None and e2m.as_text(None) is None,
      "une valeur absente ne publie rien")

gw = devices["gateway"]
by_key = {s.key: s for s in gw.sensors}
check(by_key["prod_w"].render(by_key["prod_w"].getter(data)) == "1963",
      "la production instantanée est lue correctement")
check(by_key["ct_prod_flags"].render(by_key["ct_prod_flags"].getter(data)) == "-",
      "les anomalies du compteur production sont lisibles")

phase = devices["phase_l2"]
s = {x.key: x for x in phase.sensors}["prod_w"]
check(s.render(s.getter(data, phase.phase)) == "583",
      "la production de la phase L2 est lue correctement")

panel_key = sorted(k for k in devices if k.startswith("panel_"))[0]
panel = devices[panel_key]
p = {x.key: x for x in panel.sensors}
check(p["temp"].render(p["temp"].getter(panel.source(data))) == "22",
      "la température du premier panneau est lue correctement")
check(p["dc_v"].render(p["dc_v"].getter(panel.source(data))) == "38.0",
      "la tension DC du premier panneau est lue correctement")

print("\n== soutirage et injection ==")
gw_keys = {x.key: x for x in gw.sensors}
imp, exp = gw_keys["import_w"], gw_keys["export_w"]
# La fixture porte une consommation nette de +886 W : on tire sur le reseau.
check(imp.render(imp.getter(data)) == "886", "une puissance nette positive est du soutirage")
check(exp.render(exp.getter(data)) == "0", "et l injection est alors nulle")

pos = e2m._split(lambda d: 250.0, True)
neg = e2m._split(lambda d: 250.0, False)
check((pos(None), neg(None)) == (250.0, 0.0), "un flux entrant ne compte qu en soutirage")
pos2 = e2m._split(lambda d: -400.0, True)
neg2 = e2m._split(lambda d: -400.0, False)
check((pos2(None), neg2(None)) == (0.0, 400.0),
      "un flux sortant ne compte qu en injection, et en valeur absolue")
check(e2m._split(lambda d: 0, True)(None) == 0.0
      and e2m._split(lambda d: 0, False)(None) == 0.0,
      "un flux nul ne compte nulle part")
check(e2m._split(lambda d: None, True)(None) is None,
      "une valeur absente ne publie rien plutot que zero")

ph = devices["phase_l1"]
pk = {x.key: x for x in ph.sensors}
# La phase L1 de la fixture est a -761 W : elle injecte.
check(pk["export_w"].render(pk["export_w"].getter(data, "L1")) == "761"
      and pk["import_w"].render(pk["import_w"].getter(data, "L1")) == "0",
      "le partage fonctionne aussi par phase")
check(pk["prod_wh_total"].render(pk["prod_wh_total"].getter(data, "L1")) == "2887283",
      "le cumul de production par phase est lu correctement")
check(pk["grid_wh_delivered"].render(pk["grid_wh_delivered"].getter(data, "L1")) == "2595310",
      "l energie soutiree par phase vient du compteur reseau")

pmax = {x.key: x for x in panel.sensors}["w_max"]
check(pmax.render(pmax.getter(panel.source(data))) == "279",
      "la puissance crete du panneau est publiee")

print("\n== découverte ==")
check(bridge._discovery_topic(gw, "prod_w", "sensor")
      == "homeassistant/sensor/enphase_122200000001/prod_w/config",
      "le topic de découverte est bien formé")
check(gw.state_topic(by_key["prod_w"]) == "enphase/122200000001/prod_w/state",
      "le topic d'état est bien formé")
check(panel.state_topic(p["w"]).startswith("enphase/122200000001/panneau/"),
      "les panneaux ont leur propre branche de topics")

# MQTT n'autorise qu'un testament par connexion : si le temoin de liaison et
# la disponibilite vivaient sur deux topics, une mort brutale n'en corrigerait
# qu'un et l'autre resterait bloque sur « en ligne ».
check(bridge._availability_topic == "enphase/122200000001/link/state",
      "disponibilite et temoin de liaison partagent un seul topic")
check(bridge._availability_topic not in
      [d.state_topic(x) for d in devices.values() for x in d.sensors],
      "ce topic n'est celui d'aucun capteur ordinaire")

block = bridge._device_block(panel)
check(block["via_device"] == "enphase_122200000001",
      "les panneaux sont rattachés à la passerelle")
check(bridge._device_block(gw).get("via_device") is None,
      "la passerelle n'est rattachée à rien")

print("\n== nommage des panneaux ==")
check(devices[panel_key].name == "Panneau 1",
      "les panneaux sont numérotés par numéro de série croissant")
serials = sorted(data.inverters)
bridge2, _ = build(panel_names=f"{serials[0]}:Toiture Est, {serials[1]}:Toiture Ouest")
named = {d.name for d in bridge2._devices.values()}
check("Toiture Est" in named and "Toiture Ouest" in named,
      "un nom de panneau explicite est respecté")
check("Panneau 3" in named,
      "les panneaux non nommés gardent leur numéro")
check(e2m.Bridge._parse_panel_names("  a:Un , b:Deux ,, mauvais ") ==
      {"a": "Un", "b": "Deux"},
      "le mapping tolère espaces et entrées mal formées")

print("\n== champs de panneaux ==")
check(not [k for k in devices if k.startswith("group_")],
      "sans configuration, aucun champ n est cree")

sn = sorted(data.inverters)
bg, _ = build(panel_groups=f"Champ 1 - A:{sn[0]}+{sn[1]}, Champ 2 - B:{sn[7]}+{sn[8]}+{sn[9]}")
groupes = {k: v for k, v in bg._devices.items() if k.startswith("group_")}
check(len(groupes) == 2, "deux champs declares donnent deux appareils")
check(sum(len(d.sensors) for d in bg._devices.values()) + 1 == 144 + 8,
      "chaque champ ajoute 4 entites")

g = bg._devices["group_champ_1_a"]
gs = {x.key: x for x in g.sensors}
membres = g.source(data)
check(len(membres) == 2, "le champ ne retient que ses panneaux")
check(gs["w"].render(gs["w"].getter(membres)) == "36",
      "la puissance du champ est la somme de ses panneaux (18+18)")
check(gs["wh_today"].render(gs["wh_today"].getter(membres)) == "85",
      "la production du jour est sommee (42+43)")
check(gs["w_max"].render(gs["w_max"].getter(membres)) == "558",
      "la puissance max du champ est la somme des cretes (2 x 279)")
check(g.name == "Champ 1 - A"
      and g.state_topic(gs["w"]).endswith("champ/champ_1_a/w/state"),
      "le nom du champ est respecte et son topic est en ardoise")
check(bg._device_block(g)["via_device"] == "enphase_122200000001",
      "les champs sont rattaches a la passerelle")

g2 = bg._devices["group_champ_2_b"]
check(len(g2.source(data)) == 3, "un champ de trois panneaux en retient trois")

# Une somme silencieusement amputee serait pire qu une erreur visible.
bmix, _ = build(panel_groups=f"Melange:{sn[0]}+000000000000")
check(len(bmix._devices["group_melange"].source(data)) == 1,
      "un numero de serie inconnu est ecarte de la somme")
bko, _ = build(panel_groups="Fantome:000000000000+111111111111")
check(not [k for k in bko._devices if k.startswith("group_")],
      "un champ dont aucun panneau n existe est ignore")
bdup, _ = build(panel_groups=f"Champ:{sn[0]}, Champ:{sn[1]}")
dups = [k for k in bdup._devices if k.startswith("group_")]
# Sans garde, le second champ ecraserait le premier dans le dictionnaire :
# toujours un seul appareil, mais avec les mauvais panneaux.
check(len(dups) == 1 and
      [i.serial_number for i in bdup._devices[dups[0]].source(data)] == [sn[0]],
      "un nom de champ en double garde la premiere declaration, sans ecrasement")

check(e2m.Bridge._parse_panel_groups(" A:1+2 , B:3 ,, casse ")
      == [("A", ["1", "2"]), ("B", ["3"])],
      "l analyseur tolere espaces et entrees mal formees")
check(e2m._grp("x")([Obj({"x": 5}), Obj({}), Obj({"x": 7})]) == 12,
      "un panneau sans valeur est ignore, pas compte pour zero")
check(e2m._grp("x")([Obj({}), Obj({})]) is None,
      "un champ dont aucun panneau n a de valeur ne publie rien")
check(e2m.slugify("Champ 1 - A") == "champ_1_a",
      "le nom de champ devient une ardoise propre")

print("\n== options ==")
b3, _ = build(publish_panels="false")
check(not [k for k in b3._devices if k.startswith("panel_")],
      "publish_panels=false retire les panneaux")
b4, _ = build(publish_phases="false")
check(not [k for k in b4._devices if k.startswith("phase_")],
      "publish_phases=false retire les phases")
os.environ["MQTT_TOPIC_PREFIX"] = "solaire"
os.environ["ENVOY_POLL_INTERVAL"] = "120"
cfg = e2m.load_config("/inexistant.conf")
check(cfg.get("mqtt", "topic_prefix") == "solaire",
      "une variable d'environnement l'emporte sur le fichier")
check(e2m.Bridge(cfg)._poll == 120, "la periode de releve est reprise")
del os.environ["MQTT_TOPIC_PREFIX"], os.environ["ENVOY_POLL_INTERVAL"]
cfg2 = e2m.load_config("/inexistant.conf")
check(e2m.Bridge(cfg2)._poll == 60,
      "un fichier absent laisse des valeurs par defaut utilisables")
os.environ["ENVOY_POLL_INTERVAL"] = "3"
check(e2m.Bridge(e2m.load_config("/inexistant.conf"))._poll == 15,
      "une periode trop courte est relevee au plancher de 15 s")
del os.environ["ENVOY_POLL_INTERVAL"]

print(f"\n{total} entités construites sur {len(devices)} appareils")
if failures:
    print(f"\n{len(failures)} échec(s) :")
    for failure in failures:
        print(f"  - {failure}")
    sys.exit(1)
print("Tout est vert.")
