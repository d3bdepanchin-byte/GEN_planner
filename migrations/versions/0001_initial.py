"""initial

Revision ID: 0001
Revises: 
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('employee',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('full_name', sa.Text(), nullable=False),
    sa.Column('name_variants', postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('email', sa.Text(), nullable=True),
    sa.Column('role', sa.Text(), nullable=True),
    sa.Column('department', sa.Text(), nullable=True),
    sa.Column('manager_id', sa.Integer(), nullable=True),
    sa.Column('access_level', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['manager_id'], ['employee.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('job',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('attempts', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('run_after', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('setting',
    sa.Column('key', sa.Text(), nullable=False),
    sa.Column('value', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('changed_by', sa.Text(), nullable=True),
    sa.Column('changed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('key')
    )
    op.create_table('source',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('path', sa.Text(), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('received_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('meeting_date', sa.Date(), nullable=True),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('closed', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('summary', sa.Text(), nullable=True),
    sa.Column('flags', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('sha256')
    )
    op.create_table('channel_id',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('employee_id', sa.Integer(), nullable=False),
    sa.Column('channel', sa.Text(), nullable=False),
    sa.Column('external_id', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['employee_id'], ['employee.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('channel', 'external_id')
    )
    op.create_table('review_file',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('employee_id', sa.Integer(), nullable=True),
    sa.Column('path', sa.Text(), nullable=False),
    sa.Column('task_numbers', postgresql.ARRAY(sa.Integer()), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('due_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('reminded_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('escalated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('returned_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('message_id', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['employee_id'], ['employee.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('task',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('number', sa.Integer(), nullable=False),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('type', sa.Text(), nullable=False),
    sa.Column('priority', sa.Text(), nullable=True),
    sa.Column('goal_2027', sa.Text(), nullable=True),
    sa.Column('author_id', sa.Integer(), nullable=True),
    sa.Column('owner_id', sa.Integer(), nullable=True),
    sa.Column('coexecutor_ids', postgresql.ARRAY(sa.Integer()), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('due_date', sa.Date(), nullable=True),
    sa.Column('control_date', sa.Date(), nullable=True),
    sa.Column('done_criteria', sa.Text(), nullable=True),
    sa.Column('source_id', sa.Integer(), nullable=True),
    sa.Column('timecode', sa.Text(), nullable=True),
    sa.Column('quote', sa.Text(), nullable=True),
    sa.Column('confidence', sa.Text(), nullable=True),
    sa.Column('confidence_reason', sa.Text(), nullable=True),
    sa.Column('questions', postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('flags', postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('source_closed', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('registry', sa.Text(), server_default='общий', nullable=False),
    sa.Column('registry_suggested', sa.Text(), nullable=True),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['author_id'], ['employee.id'], ),
    sa.ForeignKeyConstraint(['owner_id'], ['employee.id'], ),
    sa.ForeignKeyConstraint(['source_id'], ['source.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('number')
    )
    op.create_table('task_edit',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('task_number', sa.Integer(), nullable=False),
    sa.Column('change', sa.Text(), nullable=False),
    sa.Column('quote', sa.Text(), nullable=True),
    sa.Column('timecode', sa.Text(), nullable=True),
    sa.Column('source_id', sa.Integer(), nullable=True),
    sa.Column('status', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['source_id'], ['source.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('token',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('token', sa.Text(), nullable=False),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('employee_id', sa.Integer(), nullable=True),
    sa.Column('source_id', sa.Integer(), nullable=True),
    sa.Column('surface', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['employee_id'], ['employee.id'], ),
    sa.ForeignKeyConstraint(['source_id'], ['source.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('source_id', 'token', postgresql_nulls_not_distinct=True)
    )
    op.create_index('uq_token_employee_permanent', 'token', ['employee_id'], unique=True,
                    postgresql_where=sa.text('source_id IS NULL'))
    op.create_table('history',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('task_id', sa.Integer(), nullable=False),
    sa.Column('event', sa.Text(), nullable=False),
    sa.Column('old', sa.Text(), nullable=True),
    sa.Column('new', sa.Text(), nullable=True),
    sa.Column('actor', sa.Text(), nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['task_id'], ['task.id'], ),
    sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    op.drop_table('history')
    op.drop_table('token')
    op.drop_table('task_edit')
    op.drop_table('task')
    op.drop_table('review_file')
    op.drop_table('channel_id')
    op.drop_table('source')
    op.drop_table('setting')
    op.drop_table('job')
    op.drop_table('employee')
