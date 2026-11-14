// Copyright Epic Games, Inc. All Rights Reserved.

#pragma once

#include "CoreMinimal.h"
#include "Modules/ModuleManager.h"

class FExtender;
class FMenuBuilder;

DECLARE_LOG_CATEGORY_EXTERN(LogCarlaDigitalTwinsTool, Log, All);

class FCarlaDigitalTwinsToolModule : public IModuleInterface
{
public:

	/** IModuleInterface implementation */
	virtual void StartupModule() override;
	virtual void ShutdownModule() override;

private:
	void AddMenuEntry(FMenuBuilder& Builder);
	void OpenTrafficLightToolTab();

	TSharedPtr<FExtender> MenuExtender;
	bool bTabSpawnerRegistered = false;
};
