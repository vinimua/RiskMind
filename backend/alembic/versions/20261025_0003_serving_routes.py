"""Explicit serving acceptance, versioned routes and activation history."""

from alembic import op
import sqlalchemy as sa

revision = "20261025_0003"
down_revision = "20261024_0002"
branch_labels = None
depends_on = None


def _build_fk():
    return sa.ForeignKeyConstraint(
        ["build_id", "index_profile", "collection"],
        ["knowledge_collection_builds.build_id", "knowledge_collection_builds.index_profile",
         "knowledge_collection_builds.collection"],
    )


def _route_columns():
    return [
        sa.Column("index_profile", sa.String(64), nullable=False),
        sa.Column("collection", sa.String(255), nullable=False),
        sa.Column("build_id", sa.String(128), nullable=False),
        sa.Column("route_version", sa.Integer(), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activated_by", sa.String(128), nullable=False),
    ]


def upgrade():
    op.create_unique_constraint("uq_knowledge_collections_profile", "knowledge_collections", ["collection", "index_profile"])
    op.create_table(
        "knowledge_collection_builds",
        sa.Column("build_id", sa.String(128), primary_key=True),
        sa.Column("collection", sa.String(255), nullable=False),
        sa.Column("index_profile", sa.String(64), nullable=False),
        sa.Column("source_chunk_count", sa.Integer(), nullable=False),
        sa.Column("authoritative_snapshot_digest", sa.String(64), nullable=False),
        sa.Column("validation_digest", sa.String(64), nullable=False),
        sa.Column("build_complete", sa.Boolean(), nullable=False),
        sa.Column("validation_status", sa.String(16), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["collection", "index_profile"], ["knowledge_collections.collection", "knowledge_collections.index_profile"]),
        sa.UniqueConstraint("build_id", "index_profile", "collection", name="uq_knowledge_collection_build_identity"),
        sa.UniqueConstraint("collection", name="uq_knowledge_collection_build_collection"),
        sa.CheckConstraint("source_chunk_count >= 0", name="ck_collection_build_count"),
        sa.CheckConstraint("build_complete AND validation_status = 'PASSED'", name="ck_collection_build_passed"),
    )
    op.create_table(
        "knowledge_serving_activations",
        sa.Column("idempotency_key", sa.String(128), primary_key=True),
        *_route_columns(), _build_fk(),
        sa.UniqueConstraint("index_profile", "route_version", name="uq_serving_activation_version"),
        sa.CheckConstraint("route_version > 0", name="ck_serving_activation_version"),
    )
    op.create_table(
        "knowledge_serving_routes", *_route_columns(), _build_fk(),
        sa.PrimaryKeyConstraint("index_profile"),
        sa.ForeignKeyConstraint(["index_profile", "route_version"],
                                ["knowledge_serving_activations.index_profile", "knowledge_serving_activations.route_version"]),
    )


def downgrade():
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM knowledge_collection_builds)
           OR EXISTS (SELECT 1 FROM knowledge_serving_activations)
        THEN RAISE EXCEPTION 'cannot discard serving acceptance or activation history';
        END IF;
    END $$""")
    op.drop_table("knowledge_serving_routes")
    op.drop_table("knowledge_serving_activations")
    op.drop_table("knowledge_collection_builds")
    op.drop_constraint("uq_knowledge_collections_profile", "knowledge_collections", type_="unique")
