import sys
import os

# 添加项目目录到Python路径
project_dir = os.path.abspath(os.path.dirname(__file__))
if project_dir not in sys.path:
    sys.path.append(project_dir)
    print(f'添加项目目录到Python路径: {project_dir}')

# 尝试导入并运行web_ui.run
if __name__ == '__main__':
    try:
        from tgcf.web_ui import run
        print('成功导入tgcf.web_ui.run模块')
        run.main()
    except ImportError as e:
        print(f'导入tgcf.web_ui.run失败: {e}')
        sys.exit(1)
    except Exception as e:
        print(f'运行web UI时出错: {e}')
        sys.exit(1)