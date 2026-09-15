"""Initial evidence graph, lexical index, and backend-only Supabase access."""

from alembic import op
from sqlalchemy import text

from ingestion.tables import Base

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    Base.metadata.create_all(bind)
    if bind.dialect.name == "postgresql":
        op.execute(
            "CREATE INDEX ix_chunks_lexical ON chunks USING gin (to_tsvector('english', retrieval_text))"
        )
        for name in Base.metadata.tables:
            op.execute(f'ALTER TABLE "{name}" ENABLE ROW LEVEL SECURITY')
        # These tables are accessed through a backend owner connection, never the public Data API.
        for role in ("anon", "authenticated"):
            if bind.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname=:role"), {"role": role}
            ).scalar():
                for name in Base.metadata.tables:
                    op.execute(f'REVOKE ALL ON TABLE "{name}" FROM "{role}"')


def downgrade():
    Base.metadata.drop_all(op.get_bind())
