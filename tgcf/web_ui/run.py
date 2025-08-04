import os
import sys
from importlib import resources
import logging

# 配置日志
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

import tgcf.web_ui as wu
from tgcf.config import CONFIG, get_SESSION, write_config

# 确保配置已加载
if not CONFIG:
    logger.info("配置未加载，尝试创建默认配置")
    write_config()
    get_SESSION()

# 直接设置web_ui目录路径为已知的正确路径
package_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)))
logger.info(f"使用web_ui目录: {package_dir}")


def main():
    try:
        logger.info(f"当前工作目录: {os.getcwd()}")
        logger.info(f"web_ui目录: {package_dir}")
        path = os.path.join(package_dir, "0_👋_Hello.py")
        logger.info(f"要运行的Streamlit文件: {path}")

        # 检查文件是否存在
        if not os.path.exists(path):
            logger.error(f"文件不存在: {path}")
            # 尝试使用相对路径
            path = os.path.join("tgcf", "web_ui", "0_👋_Hello.py")
            logger.info(f"尝试使用相对路径: {path}")
            if not os.path.exists(path):
                logger.error(f"文件仍不存在: {path}")
                sys.exit(1)

        # 设置Streamlit环境变量
        os.environ["STREAMLIT_THEME_BASE"] = CONFIG.theme if hasattr(CONFIG, 'theme') else "light"
        os.environ["STREAMLIT_BROWSER_GATHER_USAGE_STATS"] = "false"
        os.environ["STREAMLIT_SERVER_HEADLESS"] = "false"  # 设置为false以便在浏览器中打开
        os.environ["STREAMLIT_SERVER_PORT"] = "8501"  # 指定端口

        logger.info("启动Streamlit应用...")
        # 使用subprocess模块运行Streamlit命令，以获取更好的错误处理
        import subprocess
        result = subprocess.run(
            ["streamlit", "run", path],
            capture_output=True,
            text=True,
            cwd=os.getcwd()
        )

        logger.info(f"Streamlit输出: {result.stdout}")
        if result.stderr:
            logger.error(f"Streamlit错误: {result.stderr}")

        if result.returncode != 0:
            logger.error(f"Streamlit退出代码: {result.returncode}")
            sys.exit(result.returncode)

    except Exception as e:
        logger.error(f"运行Web UI时出错: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
