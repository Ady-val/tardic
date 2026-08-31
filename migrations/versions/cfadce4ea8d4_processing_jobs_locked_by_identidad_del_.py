"""processing_jobs.locked_by: identidad del worker que tiene el lease

Columna nueva, nullable: las filas existentes quedan con NULL y siguen
funcionando igual (un job viejo sin firma solo se recupera por lease vencido,
que es exactamente el comportamiento anterior). Por eso no hace falta backfill
ni ventana de mantenimiento.

Revision ID: cfadce4ea8d4
Revises: ac3c1a4edbaa
Create Date: 2026-08-25 12:36:25.462618

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'cfadce4ea8d4'
down_revision: str | Sequence[str] | None = 'ac3c1a4edbaa'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Sin índice propio a propósito: las consultas que la usan filtran primero
    # por status='RUNNING' (ya indexado) y ahí caben, como mucho, tantas filas
    # como workers haya. Un índice más sería costo de escritura sin beneficio.
    op.add_column('processing_jobs', sa.Column('locked_by', sa.String(length=128), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('processing_jobs', 'locked_by')
