"""empty message

Revision ID: 5db306adc6cc
Revises: a645df168b55
Create Date: 2018-03-18 21:34:57.446692

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "5db306adc6cc"
down_revision = "a645df168b55"
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column(
        "hooks", sa.Column("commit_sha", sa.String(length=256), nullable=True)
    )
    op.create_index(op.f("ix_hooks_commit_sha"), "hooks", ["commit_sha"], unique=False)
    op.add_column(
        "hooks_version",
        sa.Column(
            "commit_sha", sa.String(length=256), autoincrement=False, nullable=True
        ),
    )
    op.create_index(
        op.f("ix_hooks_version_commit_sha"),
        "hooks_version",
        ["commit_sha"],
        unique=False,
    )
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_index(op.f("ix_hooks_version_commit_sha"), table_name="hooks_version")
    op.drop_column("hooks_version", "commit_sha")
    op.drop_index(op.f("ix_hooks_commit_sha"), table_name="hooks")
    op.drop_column("hooks", "commit_sha")
    # ### end Alembic commands ###
