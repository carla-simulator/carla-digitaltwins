# Build Instructions for CARLA Digital Twins Plugin (UE 5)

## Prerequisites

- **Unreal Engine 5 installed**
- **Git installed**
- **Operating System**: Linux or Windows
- **Enable Python Editor Script Plugin**  
  To enable:
  1. Open **Unreal Engine 5**.
  2. Go to **Edit** > **Plugins**.
  3. In the **Scripting** section, find **"Python Editor Script Plugin"**.
  4. Check the box to **enable** it.
  5. Restart the editor when prompted.

---

## Common Setup Steps (Linux & Windows)

1. Create a new Unreal Engine 5 project. It **must** be a blank C++ project. We recommend removing starter content.
2. Close the Unreal Editor.
3. Open your project directory in your file explorer.
4. Inside the project root, create a folder named `Plugins` if it doesn't already exist.
5. Open a terminal (Linux) or CMD/PowerShell (Windows), navigate to the `Plugins` folder, and clone the Digital Twins plugin:

   ```bash
   git clone -b ue5-digitaltwins https://github.com/carla-simulator/carla-digitaltwins.git
   ```

6. Navigate into the cloned folder:

   ```bash
   cd carla-digitaltwins
   ```

7. Run the setup script:

   - **On Linux:**
     ```bash
     ./Setup.sh
     ```
   - **On Windows (double-click or terminal):**
     ```cmd
     Setup.bat
     ```

---

## Linux Build Instructions

1. From the project root directory, run:

   ```bash
   make <ProjectName>Editor
   ```

   > Replace `<ProjectName>` with the actual name of your Unreal project.

2. Once compiled, double click on ProjectName.uproject

### Mitsuba support

We provide a road mesh optimization pipeline using [Mitsuba](https://mitsuba.readthedocs.io/en/stable/), an inverse rendering library integrated in DigitalTwins for road refinement. This option is currently only available for Ubuntu. It requires the use of aerial images, whose processing is provided through the library [GDAL](https://github.com/OSGeo/gdal). To install it, run the following commands:

```sh
sudo apt install libgdal-dev
pip3 install gdal[numpy]=="$(gdal-config --version).*"
```

To enable the Mitsuba road optimization option, enable it in the OpenDriveToMap blueprint:

<img width="311" height="67" alt="mitsuba_flag" src="https://github.com/user-attachments/assets/2de7166c-132d-42d2-a508-3c75ab1597a6" />

Alternatively, set the `bUseMitsuba` flag to true in [OpenDriveToMap.h](https://github.com/carla-simulator/carla-digitaltwins/blob/ue5-digitaltwins/Source/CarlaDigitalTwinsTool/Public/Generation/OpenDriveToMap.h) (line 187):

```cpp
bool bUseMitsuba = true;
```

and compile the project.

---

## Windows Build Instructions

1. Right-click the `.uproject` file and select **"Generate Visual Studio project files"**.
2. Open the generated `.sln` file with **Visual Studio 2022** or any compatible version for UE5.
3. Build the solution using **Development Editor** configuration and **Win64** platform.
4. Once compiled, run the project from Visual Studio (F5) or by double-clicking the `.uproject` file.

---

# How to Use the CARLA Digital Twins Tool in Unreal Engine

## Prerequisite

Make sure the **Digital Twins plugin** is correctly installed and built in your Unreal Engine 5 project, as described in the build instructions.

---

## Steps to Launch the Digital Twins Tool

1. **Open your Unreal Engine project** where the Digital Twins plugin is installed.

2. In the **Content Browser**, go to the bottom-right corner and click the **eye icon** to enable "Show Plugin Content".

   <img width="1022" alt="ContentBrowser" src="https://github.com/user-attachments/assets/acd1f2df-dac6-43ee-a1e4-904e26b9f4ee" />
   <img width="173" alt="ShowPluginContent" src="https://github.com/user-attachments/assets/0cdfd612-2e91-412f-abd6-5509ae2e8b8f" />

3. On the **left panel**, a new section will appear labeled `DigitalTwins Content`. Expand it.

   <img width="239" alt="CarlaDigitalTwinsContent" src="https://github.com/user-attachments/assets/c401ed92-fac0-4c31-94d0-9212ae742e27" />

4. Inside that folder, locate the file named:  
   **`UW_DigitalTwins`**

5. **Right-click** on `UW_DigitalTwins` and select:

   ```
   Run Editor Utility Widget
   ```

   <img width="290" alt="RunUtilityWidget" src="https://github.com/user-attachments/assets/1476d92e-0287-4ca7-b8b6-6bf817fef831" />

---

## Importing a Real Map from OpenStreetMap

6. Once the tool launches, a UI will appear with **three sections**:

   - **Filename** – your custom name for the map
   - **OSM URL** – the URL from OpenStreetMap
   - **LocalFilePath** – optional local saving path

7. Go to [https://www.openstreetmap.org](https://www.openstreetmap.org).

8. Search and zoom into the **area** you want to replicate.

9. Click the **Export** button on the top menu, the one in the upper part of the window which is between other buttons as shown in the following screenshot:
   ![image](https://github.com/user-attachments/assets/e6bbc00b-b30c-48f8-80ab-34a6419b3555)

10. On the left side of the window screen, find the text:
    **“Overpass API”**  
    Right-click the link and select **“Copy link address”**.

    ![image](https://github.com/user-attachments/assets/a51d849a-55e3-49ca-95c8-d96c75692e9d)

11. Go back to the Digital Twins tool in Unreal:

    - Paste the copied URL into the **OSMURL** field.
    - Enter a desired name into the **Filename** field.

12. Click the **Generate** button.

---

## Generation and Preview

13. Unreal Engine might seem frozen — **don't worry**, it's processing the data.

    - You can check the progress in the **Output Log**.

14. Once the generation is complete, click **Play** to explore your generated digital twin of the map inside the Unreal environment.

---

## Importing the Generated Map into Another Unreal Project

Once the map generation process is complete:

1. **Close the Unreal Editor**.

2. Open your **file explorer** and navigate to the `Plugins` folder of the project where you originally generated the map.

3. Inside the `Plugins` folder, you will see a plugin folder named after the **map name** you entered in the Digital Twins UI.

4. **Copy that folder**.

5. Go to the target Unreal project (the one where you want to use the map) and paste the copied folder into its own `Plugins` directory.

6. Launch the target project. Unreal Engine will detect the plugin automatically and compile it if necessary.

> Note: **Make sure the target project is also using the same Unreal Engine version and build to avoid compatibility issues.**
