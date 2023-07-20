"""empty message

Revision ID: 9ec01c7ff255
Revises: c8fe63844dd6
Create Date: 2023-02-24 11:27:00.215325

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '9ec01c7ff255'
down_revision = 'c8fe63844dd6'
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column('project_app_releases', sa.Column('build_job_id', sa.String(length=64), nullable=True))
    op.add_column('project_app_releases_version', sa.Column('build_job_id', sa.String(length=64), autoincrement=False, nullable=True))
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_column('project_app_releases_version', 'build_job_id')
    op.drop_column('project_app_releases', 'build_job_id')
    # ### end Alembic commands ###