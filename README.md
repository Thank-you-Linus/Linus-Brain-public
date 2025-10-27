# Linus Brain - AI Learning for Home Assistant

[![GitHub Release](https://img.shields.io/badge/version-0.1.0-blue.svg)](https://github.com/Thank-you-Linus/Linus-Brain)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![HACS](https://img.shields.io/badge/HACS-Default-orange.svg)](https://hacs.xyz)

**Linus Brain** est une intégration personnalisée pour Home Assistant qui transforme votre maison en un système intelligent qui apprend de vos habitudes.

> *"Le cerveau intelligent et apprenant pour votre maison"*

---

## 🎯 Qu'est-ce que Linus Brain ?

Linus Brain agit comme un **pont IA** entre votre Home Assistant et un système d'apprentissage cloud (Supabase). Il collecte automatiquement les signaux de présence de votre maison, les analyse, et génère des règles d'automatisation intelligentes basées sur vos patterns de vie.

### 🌟 Fonctionnalités principales

- 🏠 **Détection automatique** des capteurs de présence, mouvement, média et luminosité
- 📊 **Suivi d'activité intelligent** - Distingue 3 niveaux d'activité : absence, présence (<60s) et occupation (≥60s)
- 💡 **Apprentissage contextuel des lumières** - Capture les actions manuelles avec leur contexte d'activité
- 🔄 **Automatisations basées sur l'activité** - Commutateurs générés uniquement pour les zones éligibles (lumière + capteur)
- 🎯 **Sélecteurs d'entités génériques** - Règles qui s'adaptent automatiquement à chaque zone (domain + device_class + area)
- ☁️ **Synchronisation cloud** temps réel + heartbeat périodique vers Supabase
- 🤖 **Infrastructure prête pour l'IA** pour générer des automatisations intelligentes
- 📈 **Capteurs diagnostiques** pour suivre le fonctionnement de l'intégration
- 🎛️ **Configuration via UI** simple et intuitive
- 🏘️ **Support multi-instances** pour gérer plusieurs installations Home Assistant

---

## 📚 Documentation

- **[Guide de démarrage rapide](custom_components/linus_brain/QUICKSTART.md)** - Installation et configuration en 5 étapes
- **[Documentation complète](custom_components/linus_brain/README.md)** - Architecture, fonctionnalités détaillées et API
- **[Format des règles](custom_components/linus_brain/RULE_FORMAT.md)** - Guide complet sur les sélecteurs d'entités génériques
- **[Configuration](custom_components/linus_brain/CONFIGURATION.md)** - Configuration avancée et personnalisation
- **[Code source](custom_components/linus_brain/)** - Commenté et documenté pour faciliter la compréhension

---

## 🚀 Installation rapide

### Via HACS (recommandé)

1. Ouvrir HACS → Intégrations
2. Menu à trois points → "Dépôts personnalisés"
3. Ajouter : `https://github.com/Thank-you-Linus/Linus-Brain`
4. Chercher "Linus Brain" et installer
5. Redémarrer Home Assistant

### Installation manuelle

1. Télécharger le dossier `custom_components/linus_brain`
2. Copier dans `config/custom_components/` de votre Home Assistant
3. Redémarrer Home Assistant

**Puis :** Configuration → Intégrations → "Ajouter" → "Linus Brain"

### ⚙️ Configuration de la clé API Supabase

L'intégration fonctionne avec les deux types de clés Supabase :

- **Clé `anon` (recommandée pour développement)** 
  - ✅ Rate limits intégrés pour la protection
  - ✅ Plus sûre en cas de fuite accidentelle
  - ⚠️ Moins de permissions (suffisant pour cette intégration)

- **Clé `service_role` (recommandée pour production)**
  - ✅ Accès complet à la base de données
  - ✅ Performances optimales
  - ⚠️ **À sécuriser absolument** (ne jamais exposer côté client)

💡 **Pour démarrer :** Utilisez la clé `anon` trouvée dans : Supabase Dashboard → Settings → API → "anon public"

Consultez le [guide complet d'installation](custom_components/linus_brain/QUICKSTART.md) pour les détails.

---

## 🏗️ Architecture

```
Home Assistant → Linus Brain → Supabase → Agent IA → Règles → Home Assistant
```

1. **Collecte** : Linus Brain surveille les capteurs et actions utilisateur de votre maison
2. **Suivi d'activité** : Distingue les niveaux d'activité (absence, présence <60s, occupation ≥60s)
3. **Apprentissage contextuel** : Capture les actions lumières avec activité, durée et contexte environnemental
4. **Transmission** : Envoie les données enrichies à Supabase pour analyse
5. **Analyse IA** : Un agent IA analyse les patterns d'activité (à venir)
6. **Règles génériques** : Les règles utilisent des sélecteurs génériques (domain/device_class/area) qui s'adaptent à chaque zone
7. **Automatisations** : Création de commutateurs pour zones éligibles (lumière + capteur présence)

---

## 📊 Exemple de données collectées

### Données de présence
```json
{
  "room": "salon",
  "timestamp": "2025-10-22T21:32:12Z",
  "entities": {
    "motion": "on",
    "presence": "off",
    "media": "playing",
    "luminosity": 24.5
  },
  "presence_score": 0.75
}
```

### Actions lumières (apprentissage contextuel)
```json
{
  "entity_id": "light.cuisine",
  "area": "cuisine",
  "action_type": "brightness",
  "timestamp": "2025-10-22T19:15:00Z",
  "activity": "occupation",
  "activity_duration": 127.3,
  "state": {
    "brightness": 204,
    "color_temp": 370
  },
  "context": {
    "presence_detected": true,
    "illuminance": 12.3,
    "sun_elevation": -8.5,
    "hour": 19,
    "day_of_week": 2
  }
}
```

**Niveaux d'activité** :
- `none` : Aucune présence détectée
- `presence` : Présence détectée depuis moins de 60 secondes
- `occupation` : Présence continue depuis 60 secondes ou plus

---

## 🛠️ Développement

Ce projet est conçu pour être transparent et modulaire. Consultez les fichiers sources avec leurs commentaires détaillés :

- `__init__.py` - Point d'entrée de l'intégration
- `coordinator.py` - Gestion des mises à jour périodiques
- `utils/area_manager.py` - Agrégation des données par pièce
- `utils/event_listener.py` - Écoute des changements d'état en temps réel
- `utils/supabase_client.py` - Client HTTP asynchrone pour Supabase
- `utils/rule_engine.py` - Moteur de règles IA (placeholder)

### Tests locaux

```bash
# Cloner le dépôt
git clone https://github.com/Thank-you-Linus/Linus-Brain.git

# Créer un environnement de développement
python3 -m venv venv
source venv/bin/activate

# Installer Home Assistant
pip install homeassistant

# Lier l'intégration
mkdir -p ~/.homeassistant/custom_components
ln -s $(pwd)/custom_components/linus_brain ~/.homeassistant/custom_components/

# Lancer Home Assistant
hass -c ~/.homeassistant
```

---

## 🗺️ Roadmap

- [x] **v0.1.0** - Collecte et synchronisation des données de présence
- [x] **v0.1.1** - Apprentissage des patterns de contrôle des lumières
- [x] **v0.1.2** - Support multi-instances Home Assistant
- [x] **v0.1.3** - Système d'automatisation basé sur l'activité avec suivi de durée
- [ ] **v0.2.0** - Intégration IA et génération de règles contextuelles
- [ ] **v0.3.0** - Interface utilisateur avancée et visualisations
- [ ] **v0.4.0** - Prédiction de scènes lumineuses et optimisation continue

---

## 🤝 Contribution

Les contributions sont les bienvenues ! 

1. Fork le projet
2. Créer une branche (`git checkout -b feature/amazing`)
3. Commit les changements (`git commit -m 'Add amazing feature'`)
4. Push vers la branche (`git push origin feature/amazing`)
5. Ouvrir une Pull Request

---

## 📄 Licence

Ce projet est sous licence MIT. Voir [LICENSE](LICENSE) pour plus de détails.

---

## 🙏 Remerciements

Créé avec ❤️ par [@Thank-you-Linus](https://github.com/Thank-you-Linus) pour la communauté Home Assistant.

---

## 📞 Support

- **Issues** : [GitHub Issues](https://github.com/Thank-you-Linus/Linus-Brain/issues)
- **Discussions** : [GitHub Discussions](https://github.com/Thank-you-Linus/Linus-Brain/discussions)

---

**Linus Brain - Votre maison qui apprend et s'adapte à vous.** 🏠🧠
