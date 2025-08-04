import sys
import os
import subprocess
import logging

# 配置日志
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

def main():
    try:
        # 添加项目目录到Python路径
        project_dir = os.path.abspath(os.path.dirname(__file__))
        if project_dir not in sys.path:
            sys.path.append(project_dir)
            logger.info(f'添加项目目录到Python路径: {project_dir}')

        # 查找hello文件（使用os.listdir和通配符匹配）
        web_ui_dir = os.path.join(project_dir, 'tgcf', 'web_ui')
        hello_files = [f for f in os.listdir(web_ui_dir) if f.startswith('0_') and f.endswith('.py')]

        if not hello_files:
            logger.error('未找到hello文件')
            sys.exit(1)

        # 取第一个匹配的文件
        hello_file = os.path.join(web_ui_dir, hello_files[0])
        logger.info(f'找到hello文件: {hello_file}')

        # 使用Python -m streamlit.run方式运行
        logger.info('启动Streamlit应用...')
        process = subprocess.Popen(
            [sys.executable, '-m', 'streamlit', 'run', hello_file],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        # 实时打印输出
        while True:
            output = process.stdout.readline()
            if output == '' and process.poll() is not None:
                break
            if output:
                logger.info(f'Streamlit输出: {output.strip()}')

        # 打印错误（如果有）
        stderr = process.stderr.read()
        if stderr:
            logger.error(f'Streamlit错误: {stderr}')

        if process.returncode != 0:
            logger.error(f'Streamlit退出代码: {process.returncode}')
            sys.exit(process.returncode)

    except Exception as e:
        logger.error(f'运行web UI时出错: {e}')
        sys.exit(1)

if __name__ == '__main__':
    main()