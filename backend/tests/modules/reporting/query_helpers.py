"""既有读取合同测试先显式推进真实派生Worker，专门的延迟测试直接调用生产查询。"""

from app.modules.reporting.read_models.projector import rebuild_pending


def finish_projections(session):
    session.flush()
    while rebuild_pending(session, limit=100):
        pass


def build_rows(session, **kwargs):
    from app.modules.reporting.dimension_query import build_rows as query

    finish_projections(session)
    return query(session, **kwargs)


def query_ads(session, **kwargs):
    from app.modules.reporting.queries import query_ads as query

    finish_projections(session)
    return query(session, **kwargs)
