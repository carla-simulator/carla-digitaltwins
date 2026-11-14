#include "CarlaDigitalTwinsTool.h"
#include "LevelEditor.h"
#include "Framework/Application/SlateApplication.h"
#include "Widgets/Docking/SDockTab.h"
#include "Framework/MultiBox/MultiBoxBuilder.h"
#include "TrafficLights/Widgets/TLWTrafficLightToolWidget.h"

#define LOCTEXT_NAMESPACE "FCarlaDigitalTwinsToolModule"

DEFINE_LOG_CATEGORY(LogCarlaDigitalTwinsTool);

void FCarlaDigitalTwinsToolModule::StartupModule()
{
    if (IsRunningCommandlet() || GUsingNullRHI || !FSlateApplication::IsInitialized())
    {
        return;
    }

    FLevelEditorModule& LevelEditorModule = FModuleManager::LoadModuleChecked<FLevelEditorModule>("LevelEditor");

    MenuExtender = MakeShareable(new FExtender());
    MenuExtender->AddMenuExtension("WindowLayout", EExtensionHook::After, nullptr,
        FMenuExtensionDelegate::CreateRaw(this, &FCarlaDigitalTwinsToolModule::AddMenuEntry));
    if (TSharedPtr<FExtensibilityManager> MenuExtensibility = LevelEditorModule.GetMenuExtensibilityManager())
    {
        MenuExtensibility->AddExtender(MenuExtender);
    }

    FGlobalTabmanager::Get()->RegisterNomadTabSpawner("TrafficLightToolTab", FOnSpawnTab::CreateLambda([](const FSpawnTabArgs&) {
        return SNew(SDockTab)
            .TabRole(ETabRole::NomadTab)
            [
                SNew(STrafficLightToolWidget)
            ];
    }))
    .SetDisplayName(LOCTEXT("TrafficLightToolTab", "Traffic Light Tool"))
    .SetMenuType(ETabSpawnerMenuType::Hidden);
    bTabSpawnerRegistered = true;
}

void FCarlaDigitalTwinsToolModule::ShutdownModule()
{
    if (MenuExtender.IsValid() && FModuleManager::Get().IsModuleLoaded("LevelEditor"))
    {
        FLevelEditorModule& LevelEditorModule = FModuleManager::GetModuleChecked<FLevelEditorModule>("LevelEditor");
        if (TSharedPtr<FExtensibilityManager> MenuExtensibility = LevelEditorModule.GetMenuExtensibilityManager())
        {
            MenuExtensibility->RemoveExtender(MenuExtender);
        }
        MenuExtender.Reset();
    }

    if (bTabSpawnerRegistered && FSlateApplication::IsInitialized())
    {
        FGlobalTabmanager::Get()->UnregisterNomadTabSpawner("TrafficLightToolTab");
        bTabSpawnerRegistered = false;
    }
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
