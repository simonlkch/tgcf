import sys
import os
import subprocess
import logging
import threading

# 配置日志
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s %(message)s')
logger = logging.getLogger(__name__)


def main():
    process = None
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
        env = os.environ.copy()
        env['PYTHONUNBUFFERED'] = '1'
        process = subprocess.Popen(
            [
                sys.executable,
                '-u',
                '-m',
                'streamlit',
                'run',
                hello_file,
                '--logger.level=debug',
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )

        def stream_output(pipe):
            for raw in iter(pipe.readline, ''):
                line = raw.rstrip('\n')
                if line.strip() == '':
                    logger.info('Streamlit输出: ')
                    continue
                logger.info(f'Streamlit输出: {line}')

        t = threading.Thread(target=stream_output, args=(process.stdout,), daemon=True)
        t.start()
        process.wait()
        t.join(timeout=2)

        if process.returncode != 0:
            logger.error(f'Streamlit退出代码: {process.returncode}')
            sys.exit(process.returncode)

    except KeyboardInterrupt:
        logger.info('收到中断信号，正在关闭Streamlit...')
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        return

    except Exception as e:
        logger.error(f'运行web UI时出错: {e}')
        sys.exit(1)

if __name__ == '__main__':
    main()