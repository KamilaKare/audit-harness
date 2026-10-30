# Dashboard métier et revue humaine

Le dashboard Streamlit présente les exécutions existantes du harness et permet de lancer une nouvelle exécution ou d’enregistrer une revue. Toutes les lectures et écritures passent par FastAPI. L’interface ne recalcule pas les règles de contrôle et ne modifie pas directement SQLite.

## Démarrage local

Depuis le dossier du projet :

```sh
uv sync --locked --extra dashboard --extra dev --extra observability
```

Dans un premier Terminal :

```sh
uv run --locked --extra observability audit-harness serve
```

Dans un deuxième Terminal :

```sh
uv run --locked --extra dashboard --extra observability audit-harness dashboard
```

Ouvrir http://localhost:8501. Le backend est disponible sur http://localhost:8000/docs. Redémarrer le backend après une mise à jour du code : un ancien processus ne connaît pas les nouveaux endpoints du dashboard.

Le même `AUDIT_API_KEY` doit être configuré dans le `.env` utilisé par les deux processus. La clé LLM reste distincte (`AUDIT_LLM_API_KEY`) ; les clés Langfuse sont également distinctes. Ne pas les renseigner dans un formulaire de revue.

Paramètres optionnels dans `.env` :

```dotenv
AUDIT_DASHBOARD_API_URL=http://127.0.0.1:8000
AUDIT_DASHBOARD_TIMEOUT_SECONDS=180
AUDIT_PRICING_FILE=config/model_pricing.yaml
```

Le dashboard écoute uniquement sur `127.0.0.1` par défaut. `audit-harness dashboard --port 8502` permet de changer de port. Cette interface reste une démonstration locale sur données synthétiques : l’identité saisie par le réviseur est déclarative et la clé API est partagée.

## Parcours de démonstration

La dernière exécution est sélectionnée automatiquement ; les 50 plus récentes sont accessibles dans la barre latérale. Le bouton « Actualiser les exécutions » recharge les données. Une exécution CLI existante est visible si le serveur utilise la même base de données.

Le tableau de bord montre la population, les résultats par contrôle, les revues en attente et les analyses du modèle. Les résultats hors périmètre sont explicitement distingués : un PASS hors périmètre n’est pas une validation des preuves.

Trois boutons ouvrent directement des dossiers représentatifs :

1. **Conforme** : une transaction dont les contrôles applicables sont satisfaits.
2. **Auto-approbation** : C2 détecte qu’une même personne initie et approuve un paiement.
3. **Fournisseur récent** : C3 déclenche une alerte ; le modèle interprète une justification et la décision finale exige une revue humaine.

Dans la vue « Revue des transactions », filtrer par contrôle, résultat, entité, recherche textuelle ou revue en attente. Sélectionner une ligne ou ouvrir une transaction dans le sélecteur. Le dossier sépare les tests, les preuves collectées, l’avis du modèle et la revue humaine. Les pièces sont présentées sous une forme lisible ; les JSON bruts sont repliés dans un volet technique.

Le modèle peut recommander une justification plausible sans autoriser une exception. La confiance affichée est auto-déclarée et non calibrée. Le fournisseur `offline` est identifié comme simulateur : ses réponses ne sont pas des analyses d’un vrai LLM.

## Enregistrer une revue

Choisir une décision parmi celles autorisées par le backend, renseigner un motif et sélectionner les preuves examinées. Un contrôle déterministe déjà en échec ne peut pas devenir PASS via une revue. Seul C3 peut recevoir une exception de politique.

Le dashboard conserve la révision vue à l’ouverture du formulaire. Si une autre revue intervient, la soumission est bloquée jusqu’au rechargement explicite. Les décisions humaines et leur historique apparaissent séparément des résultats automatiques d’origine. Une requête expirée n’est jamais retentée automatiquement : actualiser pour vérifier l’état avant de relancer une action.

Le bouton d’export télécharge le dossier original et les revues. Aucun fichier de preuve local n’est ouvert à partir d’un chemin fourni par l’utilisateur : les données viennent du snapshot enregistré dans le backend.

## Mesures de performance et coût

Chaque nouvelle décision conserve un objet `execution` : durée du contrôle, tentative/réussite de l’analyse réelle, latence du modèle, identifiant du modèle effectivement retourné et tokens disponibles. Ces mesures sont capturées même si Langfuse est désactivé. Une réponse modèle invalide peut tout de même avoir consommé des tokens ; ils restent comptabilisés lorsqu’ils ont été retournés par le fournisseur.

Les anciennes exécutions, dont `live-run.json` antérieur à cette évolution, n’ont pas ces mesures. Le dashboard affiche « Non enregistré » ou « Non disponible », jamais un coût nul déduit de données manquantes. Lancer une nouvelle exécution pour obtenir ces mesures. La simulation ne consomme aucun appel LLM réel et son coût modèle est zéro.

La page « Performance & coût » et les rapports d’évaluation incluent :

- Couverture des mesures et nombre d’appels réels, réussis ou en erreur.
- Tokens d’entrée/sortie disponibles.
- Moyenne, médiane, 95e percentile et maximum des latences de contrôle et de modèle, en millisecondes.
- Coût modèle total estimé, moyenne par contrôle et coût par transaction correctement traitée selon les labels.

La durée du run mesure la boucle de contrôle ; les latences de contrôle incluent ses étapes et l’instrumentation, pas le chargement initial de la population ni la sauvegarde du run. Les percentiles utilisent une interpolation linéaire. Le coût par contrôle utilise toutes les décisions au dénominateur, y compris celles sans appel LLM. Le coût par transaction réussie inclut le coût des tâches échouées au numérateur ; s’il n’existe aucune tâche réussie, il reste indisponible.

## Configurer des tarifs

Aucun prix fournisseur n’est supposé. Le fichier `config/model_pricing.yaml` est vide par défaut. Pour des estimations, y ajouter des tarifs que vous avez vérifiés pour les identifiants exacts renvoyés par le fournisseur, puis changer la version.

Exemple **fictif**, qui n’est pas un tarif commercial :

```yaml
version: demo-pricing-1
models:
  your-exact-model-id:
    input_per_million_usd: '1.00'
    output_per_million_usd: '2.00'
```

Les tarifs sont en USD par million de tokens. Les coûts sont calculés avec Decimal, conservés dans le snapshot de décision avec la version du tarif ; le run conserve aussi le contenu et l’empreinte de la configuration tarifaire. Ils ne sont pas recalculés rétrospectivement lorsque le fichier change. Les prix standard ne tiennent pas compte des remises, tokens mis en cache ou catégories de facturation particulières. L’estimation exclut l’infrastructure et le temps humain.

Sans tarif exact, sans consommation complète, ou lorsque certaines décisions ne sont pas mesurées, le total reste indisponible. Le backend fournit séparément le sous-total connu et la couverture ; il ne présente jamais un montant partiel comme un total complet.

La concordance avec les 360 labels synthétiques est affichée séparément. Elle ne mesure pas la pertinence sémantique des justifications du LLM. L’évaluation humaine indépendante de ces justifications reste une étape ultérieure.

## API et vérification

Nouveaux endpoints protégés :

- `GET /runs?limit=50&offset=0` : liste des exécutions, sans leurs populations complètes.
- `GET /runs/{run_id}/metrics` : couverture, latences, consommation et coûts estimés.
- `POST /runs` accepte un corps facultatif `{"provider":"offline"}` ou `{"provider":"openai-compatible"}`. Sans corps, le comportement existant est conservé.
- La file de revue fournit les décisions humaines autorisées par le backend.

Les tests Streamlit `AppTest` exécutent les vrais écrans en mémoire, avec une vraie API et une base temporaire. Ils couvrent les trois dossiers, les filtres, la sauvegarde d’une revue, une modification concurrente, le lancement d’une nouvelle exécution et la lecture d’un run historique. Aucun appel à un fournisseur externe n’est requis.

```sh
uv run --locked --extra dev --extra dashboard --extra observability pytest
```

Le navigateur n’a pas été inspecté visuellement depuis l’environnement agent. Les tests d’interaction ne remplacent pas une vérification visuelle finale sur votre écran.

## Docker

```sh
docker compose --profile dashboard up --build
```

Le profil ajoute le dashboard sur http://localhost:8501 et attend que le backend soit sain. Les deux ports sont publiés uniquement sur localhost. La base est montée dans le backend ; le dashboard y accède uniquement par HTTP. La configuration Compose est validable par `docker compose config --quiet` ; le démarrage des conteneurs nécessite un Docker Engine disponible.
