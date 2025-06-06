# 🔧 Build Instructions for CARLA Digital Twins Plugin (UE 4.26)

## ✅ Prerequisites

- **Unreal Engine 4.26 installed**
- Git installed
- Operating System: **Linux** or **Windows**

---

## 🧱 Common Setup Steps (Linux & Windows)

1. **Create a new Unreal Engine 4.26 project** (Blank or Basic Code Project).
2. Close the Unreal Editor.
3. Open your project directory in your file explorer.
4. Inside the project root, create a folder named `Plugins` if it doesn't already exist.
5. Open a terminal (Linux) or CMD/PowerShell (Windows), navigate to the `Plugins` folder, and clone the Digital Twins plugin:

   ```bash
   git clone https://github.com/carla-simulator/carla-digitaltwins.git
   ```

6. Navigate into the cloned folder:

   ```bash
   cd carla-digitaltwins
   ```

7. Run the setup script:

   - **On Linux:**
     ```bash
     ./setup.sh
     ```
   - **On Windows (double-click or terminal):**
     ```cmd
     setup.bat
     ```

---

## 🐧 Linux Build Instructions

1. From the plugin or project root directory, run:

   ```bash
   make ProjectNameEditor
   ```

   > Replace `ProjectName` with the actual name of your Unreal project.

2. Once compiled, you can run the project using the binary:

   ```bash
   ./Binaries/Linux/ProjectNameEditor
   ```

---

## 🪟 Windows Build Instructions

1. Right-click the `.uproject` file and select **"Generate Visual Studio project files"**.
2. Open the generated `.sln` file with **Visual Studio 2019** or any compatible version for UE 4.26.
3. Build the solution using **Development Editor** configuration and **Win64** platform.
4. Once compiled, run the project from Visual Studio or by double-clicking the `.uproject` file.

---
