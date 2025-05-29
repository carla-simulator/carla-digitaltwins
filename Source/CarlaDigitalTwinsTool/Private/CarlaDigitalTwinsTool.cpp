#include "CarlaDigitalTwinsTool.h"
#include "LevelEditor.h"
#include "Widgets/Docking/SDockTab.h"
#include "Framework/MultiBox/MultiBoxBuilder.h"
#include "TrafficLights/Widgets/STrafficLightToolWidget.h"

#define LOCTEXT_NAMESPACE "FCarlaDigitalTwinsToolModule"

DEFINE_LOG_CATEGORY(LogCarlaDigitalTwinsTool);

void FCarlaDigitalTwinsToolModule::StartupModule()
{
    // Agregar entrada al menú de LevelEditor
    FLevelEditorModule& LevelEditorModule = FModuleManager::LoadModuleChecked<FLevelEditorModule>("LevelEditor");

    TSharedPtr<FExtender> MenuExtender = MakeShareable(new FExtender());
    MenuExtender->AddMenuExtension("WindowLayout", EExtensionHook::After, nullptr,
        FMenuExtensionDelegate::CreateRaw(this, &FCarlaDigitalTwinsToolModule::AddMenuEntry));
    LevelEditorModule.GetMenuExtensibilityManager()->AddExtender(MenuExtender);

    // Registrar la pestaña
    FGlobalTabmanager::Get()->RegisterNomadTabSpawner("TrafficLightToolTab", FOnSpawnTab::CreateLambda([](const FSpawnTabArgs&) {
        return SNew(SDockTab)
            .TabRole(ETabRole::NomadTab)
            [
                SNew(STrafficLightToolWidget)
            ];
    }))
    .SetDisplayName(LOCTEXT("TrafficLightToolTab", "Traffic Light Tool"))
    .SetMenuType(ETabSpawnerMenuType::Hidden);
}

void FCarlaDigitalTwinsToolModule::ShutdownModule()
{
    FGlobalTabmanager::Get()->UnregisterNomadTabSpawner("TrafficLightToolTab");
}

void FCarlaDigitalTwinsToolModule::AddMenuEntry(FMenuBuilder& Builder)
{
    Builder.AddMenuEntry(
        LOCTEXT("OpenTrafficLightTool", "Traffic Light Tool"),
        LOCTEXT("OpenTrafficLightToolTooltip", "Opens the Traffic Light Tool."),
        FSlateIcon(),
        FUIAction(FExecuteAction::CreateRaw(this, &FCarlaDigitalTwinsToolModule::OpenTrafficLightToolTab))
    );
}

void FCarlaDigitalTwinsToolModule::OpenTrafficLightToolTab()
{
    FGlobalTabmanager::Get()->TryInvokeTab(FTabId("TrafficLightToolTab"));
}

#undef LOCTEXT_NAMESPACE

IMPLEMENT_MODULE(FCarlaDigitalTwinsToolModule, CarlaDigitalTwinsTool)
