from open_webui.main import app
from open_webui.routers.openai import get_openai_config
from open_webui.utils.auth import get_admin_user, get_verified_user

from personal import personal_router
from extractor import router as extractor_router
from balance import balance_router

# 将扩展路由放在上游前端静态资源的兜底路由之前。
router = balance_router(get_admin_user, get_openai_config)
app.router.routes[0:0] = [*router.routes, *extractor_router.routes, *personal_router(get_verified_user).routes]
