# Icônes d'équipement pour le plugin Jeedom MQTT Discovery

Le plugin cherche l'icône d'un équipement dans
`plugins/MQTTDiscovery/data/images/custom/<imageName>.png`, où `imageName`
dérive du `model_id` publié dans la découverte.

Les appareils Zigbee récupèrent leur photo automatiquement depuis
zigbee2mqtt.io ; les autres passerelles n'ont pas d'équivalent, d'où ces
fichiers. Une icône maison ne se répare pas toute seule : garder ces
sources ici et les redéposer si elles disparaissent.

    ENPHASE-ENVOY.png    la passerelle
    ENPHASE-PHASE.png    les 3 phases
    ENPHASE-PANEL.png    les 10 micro-onduleurs
    ENPHASE-ARRAY.png    les 4 champs

Redéposer :

    docker cp ENPHASE-*.png jeedom:/var/www/html/plugins/MQTTDiscovery/data/images/custom/

Les régénérer : `python make_eq_icons.py` (nécessite Pillow).
Palette reprise des passerelles maison : fond `#0E3F5C`, glyphe `#F4F8FA`,
accents `#29B6F6`.
