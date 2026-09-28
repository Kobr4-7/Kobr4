# Maquette — Kobr4 FX

Maquette interactive du tableau de bord du bot de trading forex. Tout est simulé (cotations, positions, résultats) ; aucune connexion à un courtier.

Ouvrir `index.html` dans un navigateur.

## Écrans couverts

- **Barre d'état** : statut du bot (en marche / en pause), mode démo, heure UTC, pause et arrêt d'urgence (clôture de toutes les positions, avec confirmation).
- **Résumé du compte** : équité, P&L flottant, P&L du jour, drawdown max, taux de réussite.
- **Paires suivies** : 7 paires majeures avec cours, variation et spread ; un clic change le graphique.
- **Graphique** : chandeliers M15 / H1 / H4 / D1, EMA rapide et lente, signaux de croisement, niveaux Entrée / SL / TP des positions ouvertes, cotation vendre / acheter.
- **Stratégie** : modèle (croisement EMA, RSI, breakout), paramètres EMA, SL / TP en pips, sessions de marché, filtre d'annonces économiques.
- **Gestion du risque** : risque par trade en % du capital, limite de perte journalière, nombre de positions, marge utilisée.
- **Positions / Historique / Journal** : positions ouvertes avec clôture manuelle, trades fermés avec motif, journal des décisions du bot.
