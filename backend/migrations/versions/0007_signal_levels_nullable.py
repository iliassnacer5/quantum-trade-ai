"""Rend `signals.entry` et `signals.stop_loss` NULLABLES.

POURQUOI — « pas de trade » n'a pas de niveaux, et zéro n'est pas une réponse.

Quand la décision est HOLD (aucun trade, ou trade bloqué par les filtres de fiabilité), le moteur
n'a ni entrée, ni stop, ni objectif à proposer. Faute de pouvoir écrire NULL, il recopiait le PRIX
COURANT dans les trois colonnes et mettait le R/R à 0. La carte affichait alors
« Entrée 357.4 · Stop-Loss 357.4 · TP 357.4 · R/R 1:0 » — trois niveaux d'apparence exploitable qui
ne veulent rien dire, et qu'un lecteur pressé peut prendre pour un vrai plan de trade.

C'est la même règle que partout ailleurs dans le projet : une donnée absente vaut NULL, jamais 0.

Les lignes existantes ne sont PAS réécrites : on ne peut pas distinguer après coup un vrai niveau
d'un prix recopié, et deviner reviendrait à inventer. Elles restent telles quelles ; seules les
prédictions écrites à partir de maintenant portent NULL quand il n'y a pas de trade.

Revision ID: 0007
Revises: 0006
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def _is_nullable(table: str, column: str) -> bool:
    cols = sa.inspect(op.get_bind()).get_columns(table)
    return any(c["name"] == column and c["nullable"] for c in cols)


def upgrade() -> None:
    for column in ("entry", "stop_loss"):
        if not _is_nullable("signals", column):
            op.alter_column("signals", column, existing_type=sa.Float(), nullable=True)


def downgrade() -> None:
    # Retour en NOT NULL : impossible tant qu'il subsiste des lignes sans niveaux (les HOLD écrits
    # après cette migration). On les purge — ce sont des prédictions « pas de trade », sans valeur
    # historique de niveau, et c'est la seule façon de restaurer la contrainte sans inventer.
    for column in ("entry", "stop_loss"):
        if _is_nullable("signals", column):
            op.execute(f"DELETE FROM signals WHERE {column} IS NULL")
            op.alter_column("signals", column, existing_type=sa.Float(), nullable=False)
