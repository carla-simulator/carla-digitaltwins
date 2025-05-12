import os

# Folder where your target .cpp files live
TARGET_FOLDER = r'C:\CarlaDigitalTwins\Plugins\CarlaDigitalTwinsTool\Source\CarlaDigitalTwinsTool\Public\Carla'

# Correct macro line for Unreal Engine
PRAGMA_LINE = '#pragma UE_DISABLE_UNITY_INCLUSION\n'

def process_cpp_file(file_path):
    with open(file_path, 'r', encoding='utf-8') as file:
        lines = file.readlines()

    # Skip if already has macro
    for line in lines[:5]:  # Check first few lines
        if 'UE_DISABLE_UNITY_INCLUSION' in line:
            return False  # Already done

    # Insert macro after license or first code line
    insert_index = 0
    for i, line in enumerate(lines):
        if line.strip() and not line.startswith('//'):
            insert_index = i
            break

    lines.insert(insert_index, PRAGMA_LINE)

    with open(file_path, 'w', encoding='utf-8') as file:
        file.writelines(lines)

    return True

def main():
    count = 0
    for root, dirs, files in os.walk(TARGET_FOLDER):
        for file in files:
            if file.endswith('.cpp'):
                file_path = os.path.join(root, file)
                if process_cpp_file(file_path):
                    print(f'✅ Patched: {file_path}')
                    count += 1
            if file.endswith('.h'):
                file_path = os.path.join(root, file)
                if process_cpp_file(file_path):
                    print(f'✅ Patched: {file_path}')
                    count += 1


    print(f'\nDone! {count} files patched.')

if __name__ == '__main__':
    main()
