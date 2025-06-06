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

# 🧭 How to Use the CARLA Digital Twins Tool in Unreal Engine

## ✅ Prerequisite

Make sure the **Digital Twins plugin** is correctly installed and built in your Unreal Engine 4.26 project, as described in the build instructions.

---

## 🚀 Steps to Launch the Digital Twins Tool

1. **Open your Unreal Engine project** where the Digital Twins plugin is installed.

2. In the **Content Browser**, go to the bottom-right corner and click the **eye icon** to enable "Show Plugin Content".

   📸 *[Insert Screenshot: Content Browser with eye icon highlighted]*

3. On the **left panel**, a new section will appear labeled `DigitalTwins Content`. Expand it.

   📸 *[Insert Screenshot: Plugin Content section showing "DigitalTwins"]*

4. Navigate to the plugin content folder:

5. Inside that folder, locate the file named:  
   **`UW_DigitalTwins`**

6. **Right-click** on `UW_DigitalTwins` and select:

   ```
   Run Editor Utility Widget
   ```

   📸 *[Insert Screenshot: Right-click on UW_DigitalTwins with "Run Editor Utility Widget" selected]*

---

## 🗺️ Importing a Real Map from OpenStreetMap

7. Once the tool launches, a UI will appear with **three sections**:

   - **Filename** – your custom name for the map
   - **OSMURL** – the URL from OpenStreetMap
   - **LocalFilePath** – optional local saving path

   ![image](https://github.com/user-attachments/assets/a076addd-3275-4304-b815-27575c9766b0)


8. Go to [https://www.openstreetmap.org](https://www.openstreetmap.org).

9. Search and zoom into the **area** you want to replicate.

10. Click the **Export** button on the top menu, the one in the upper part of the window which is between other buttons. NOT THE BLUE ONE.
   
![image](https://github.com/user-attachments/assets/e6bbc00b-b30c-48f8-80ab-34a6419b3555)


11. On the left side of the window screen, find the text:
    **“Overpass API”**  
    Right-click the link and select **“Copy link address”**.

    ![image](https://github.com/user-attachments/assets/a51d849a-55e3-49ca-95c8-d96c75692e9d)


12. Go back to the Digital Twins tool in Unreal:

    - Paste the copied URL into the **OSMURL** field.
    - Enter a desired name into the **Filename** field.

13. Click the **Generate** button.

---

## ⚙️ Generation and Preview

14. Unreal Engine might seem frozen — **don't worry**, it's processing the data.

    - You can check the progress in the **Output Log**.

    📸 *[Insert Screenshot: Output Log showing map generation progress]*

15. Once the generation is complete, click **Play** to explore your generated digital twin of the map inside the Unreal environment.

---
