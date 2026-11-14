#include "TrafficLights/Widgets/TLWTrafficLightToolWidget.h"

#include "CarlaDigitalTwinsTool.h"
#include "Components/ActorComponent.h"
#include "Components/StaticMeshComponent.h"
#include "Containers/Array.h"
#include "Containers/UnrealString.h"
#include "CoreGlobals.h"
#include "Delegates/Delegate.h"
#include "DesktopPlatformModule.h"
#include "Dom/JsonObject.h"
#include "Engine/StaticMesh.h"
#include "Engine/StaticMeshActor.h"
#include "Engine/StaticMeshSocket.h"
#include "EngineUtils.h"
#include "Framework/Application/SlateApplication.h"
#include "GenericPlatform/GenericPlatformFile.h"
#include "HAL/PlatformFileManager.h"
#include "IDesktopPlatform.h"
#include "Logging/LogMacros.h"
#include "Logging/LogVerbosity.h"
#include "Materials/MaterialInstanceDynamic.h"
#include "Math/MathFwd.h"
#include "Misc/AssertionMacros.h"
#include "Misc/FileHelper.h"
#include "Misc/PackageName.h"
#include "Misc/Paths.h"
#include "ScopedTransaction.h"
#include "Selection.h"
#include "Serialization/JsonReader.h"
#include "Serialization/JsonSerializer.h"
#include "Styling/AppStyle.h"
#include "TrafficLights/TLHead.h"
#include "TrafficLights/TLLightTypeDataTable.h"
#include "TrafficLights/TLMeshFactory.h"
#include "TrafficLights/TLModule.h"
#include "TrafficLights/TLPole.h"
#include "UObject/Object.h"
#include "Widgets/Colors/SColorPicker.h"
#include "Widgets/Images/SImage.h"
#include "Widgets/Input/SButton.h"
#include "Widgets/Input/SCheckBox.h"
#include "Widgets/Input/SComboBox.h"
#include "Widgets/Input/SComboButton.h"
#include "Widgets/Input/SNumericEntryBox.h"
#include "Widgets/Input/SSlider.h"
#include "Widgets/Layout/SBorder.h"
#include "Widgets/Layout/SExpandableArea.h"
#include "Widgets/Layout/SScrollBox.h"
#include "Widgets/Layout/SSplitter.h"
#include "Widgets/SNullWidget.h"
#include "Widgets/Text/STextBlock.h"

void STrafficLightToolWidget::Construct(const FArguments& InArgs)
{
	if (StyleOptions.IsEmpty())
	{
		if (UEnum* EnumPtr = StaticEnum<ETLStyle>())
		{
			for (int32 i{0}; i < EnumPtr->NumEnums() - 1; ++i)
			{
				StyleOptions.Add(MakeShared<FString>(EnumPtr->GetDisplayNameTextByIndex(i).ToString()));
			}
		}
	}
	if (AttachmentOptions.IsEmpty())
	{
		if (UEnum* EnumPtr = StaticEnum<ETLHeadAttachment>())
		{
			for (int32 i = 0; i < EnumPtr->NumEnums() - 1; ++i)
			{
				AttachmentOptions.Add(MakeShared<FString>(EnumPtr->GetDisplayNameTextByIndex(i).ToString()));
			}
		}
	}
	if (OrientationOptions.IsEmpty())
	{
		if (UEnum* EnumPtr = StaticEnum<ETLOrientation>())
		{
			for (int32 i = 0; i < EnumPtr->NumEnums() - 1; ++i)
			{
				OrientationOptions.Add(MakeShared<FString>(EnumPtr->GetDisplayNameTextByIndex(i).ToString()));
			}
		}
	}
	if (LightTypeOptions.IsEmpty())
	{
		if (UEnum* EnumPtr = StaticEnum<ETLLightType>())
		{
			for (int32 i = 0; i < EnumPtr->NumEnums() - 1; ++i)
			{
				LightTypeOptions.Add(MakeShared<FString>(EnumPtr->GetDisplayNameTextByIndex(i).ToString()));
			}
		}
	}

#if WITH_EDITOR
	if (GEditor)
	{
		if (USelection* Sel{GEditor->GetSelectedActors()})
		{
			for (FSelectionIterator It(*Sel); It; ++It)
			{
				if (AActor * Actor{Cast<AActor>(*It)})
				{
					const FString Folder{Actor->GetFolderPath().ToString()};
					if (Folder.StartsWith(TEXT("TrafficLightActor")))
					{
						UE_LOG(LogCarlaDigitalTwinsTool, Log, TEXT("Traffic Light bake folder detected: %s"), *Folder);
						break;
					}
				}
			}
		}
	}
#endif

	ChildSlot[SNew(SSplitter)

			  + SSplitter::Slot().Value(0.4f)[BuildPoleSection()]

			  + SSplitter::Slot().Value(0.6f)[SAssignNew(PreviewViewport, STrafficLightPreviewViewport)]];
}

// ----------------------------------------------------------------------------------------------------------------
// ******************************   REBUILD
// ----------------------------------------------------------------------------------------------------------------
void STrafficLightToolWidget::InitializeDefaultLight(FTLModuleLight& Light) const
{
	const FIntPoint AtlasUV{FTLMeshFactory::GetAtlasCoordsForLightType(Light.LightType)};
	Light.U = AtlasUV.X;
	Light.V = AtlasUV.Y;

	UDataTable* DT{FTLMeshFactory::GetLightTypeMeshTable()};
	const UEnum* EnumPtr{StaticEnum<ETLLightType>()};
	if (IsValid(DT) && EnumPtr)
	{
		const FString Key{EnumPtr->GetNameStringByValue(static_cast<int64>(Light.LightType))};
		if (const FTLLightTypeRow* Row{DT->FindRow<FTLLightTypeRow>(FName(*Key), TEXT("InitializeDefaultLight"))})
		{
			Light.EmissiveColor = Row->Color;
			Light.EmissiveIntensity = Row->EmissiveIntensity;
		}
	}
}

void STrafficLightToolWidget::SyncModuleLightsWithMesh(FTLModule& Module, FEditorModule& EditorModule)
{
	const int32 NumLights{IsValid(Module.ModuleMesh) ? FTLMeshFactory::CountLedMaterials(Module.ModuleMesh) : 0};
	const int32 OldNumLights{Module.Lights.Num()};
	Module.Lights.SetNum(NumLights);
	for (int32 i{OldNumLights}; i < NumLights; ++i)
	{
		InitializeDefaultLight(Module.Lights[i]);
	}
	EditorModule.Lights.SetNum(NumLights);
}

void STrafficLightToolWidget::UpdatePreviewActor()
{
	if (!PreviewViewport.IsValid())
	{
		return;
	}
	ATrafficLightActor* TL{PreviewViewport->GetPreviewTrafficLight()};
	if (!IsValid(TL))
	{
		return;
	}
	TL->Poles = Poles;
	TL->RerunConstructionScripts();
}

void STrafficLightToolWidget::UpdatePreviewLightParameter(
	int32 PoleIndex, int32 HeadIndex, int32 ModuleIndex, int32 LightIndex)
{
	if (!Poles.IsValidIndex(PoleIndex) || !Poles[PoleIndex].Heads.IsValidIndex(HeadIndex) ||
		!Poles[PoleIndex].Heads[HeadIndex].Modules.IsValidIndex(ModuleIndex) ||
		!Poles[PoleIndex].Heads[HeadIndex].Modules[ModuleIndex].Lights.IsValidIndex(LightIndex))
	{
		return;
	}
	if (!PreviewViewport.IsValid())
	{
		return;
	}

	const FTLModuleLight& WidgetLight{Poles[PoleIndex].Heads[HeadIndex].Modules[ModuleIndex].Lights[LightIndex]};
	ATrafficLightActor* TL{PreviewViewport->GetPreviewTrafficLight()};
	if (IsValid(TL) && TL->Poles.IsValidIndex(PoleIndex) && TL->Poles[PoleIndex].Heads.IsValidIndex(HeadIndex) &&
		TL->Poles[PoleIndex].Heads[HeadIndex].Modules.IsValidIndex(ModuleIndex) &&
		TL->Poles[PoleIndex].Heads[HeadIndex].Modules[ModuleIndex].Lights.IsValidIndex(LightIndex))
	{
		FTLModuleLight& PreviewLight{TL->Poles[PoleIndex].Heads[HeadIndex].Modules[ModuleIndex].Lights[LightIndex]};
		PreviewLight.LightType = WidgetLight.LightType;
		PreviewLight.U = WidgetLight.U;
		PreviewLight.V = WidgetLight.V;
		PreviewLight.EmissiveIntensity = WidgetLight.EmissiveIntensity;
		PreviewLight.EmissiveColor = WidgetLight.EmissiveColor;

		if (IsValid(PreviewLight.LightMID))
		{
			PreviewLight.LightMID->SetScalarParameterValue(TEXT("Emissive Intensity"), PreviewLight.EmissiveIntensity);
			PreviewLight.LightMID->SetVectorParameterValue(TEXT("Emissive Color"), PreviewLight.EmissiveColor);
			PreviewLight.LightMID->SetScalarParameterValue(TEXT("Offset U"), static_cast<float>(PreviewLight.U));
			PreviewLight.LightMID->SetScalarParameterValue(TEXT("Offset V"), static_cast<float>(PreviewLight.V));
		}
		else
		{
			UpdatePreviewActor();
		}
	}
	else
	{
		UpdatePreviewActor();
	}

	PreviewViewport->Reload();
}

void STrafficLightToolWidget::Rebuild()
{
	if (!PreviewViewport.IsValid())
	{
		return;
	}

	for (int32 PoleIndex{0}; PoleIndex < EditorPoles.Num(); ++PoleIndex)
	{
		FEditorPole& EditorPole{EditorPoles[PoleIndex]};
		for (int32 HeadIndex{0}; HeadIndex < EditorPole.Heads.Num(); ++HeadIndex)
		{
			for (int32 ModuleIndex{0}; ModuleIndex < EditorPole.Heads[HeadIndex].Modules.Num(); ++ModuleIndex)
			{
				RefreshModuleMeshOptions(PoleIndex, HeadIndex, ModuleIndex);
			}
		}
	}

	UpdatePreviewActor();
	PreviewViewport->ResetFrameIfBoundsChanged();
	PreviewViewport->Reload();

	RefreshPoleList();
}

void STrafficLightToolWidget::RebuildModuleChain(FTLHead& Head)
{
	if (Head.Modules.IsEmpty())
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Warning, TEXT("RebuildModuleChain: Modules.Num()"));
		return;
	}

	static const FName EndSocketName{FName("Socket2")};
	static const FName BeginSocketName{FName("Socket1")};

	{
		FTLModule& Module{Head.Modules[0]};
		Module.Transform = FTransform::Identity;
		if (IsValid(Module.ModuleMeshComponent))
		{
			Module.ModuleMeshComponent->SetRelativeTransform(Module.Offset);
		}
	}

	for (int32 i{1}; i < Head.Modules.Num(); ++i)
	{
		const FTLModule& Prev{Head.Modules[i - 1]};
		FTLModule& Curr{Head.Modules[i]};

		if (!IsValid(Prev.ModuleMeshComponent) || !IsValid(Prev.ModuleMesh))
		{
			UE_LOG(LogCarlaDigitalTwinsTool, Warning, TEXT("Previous module mesh/component is invalid at module %d"), i);
			continue;
		}
		if (!IsValid(Curr.ModuleMeshComponent) || !IsValid(Curr.ModuleMesh))
		{
			UE_LOG(LogCarlaDigitalTwinsTool, Warning, TEXT("Missing mesh/component at module %d"), i);
			continue;
		}

		const UStaticMeshSocket* PrevSocket{Prev.ModuleMesh->FindSocket(EndSocketName)};
		const UStaticMeshSocket* CurrSocket{Curr.ModuleMesh->FindSocket(BeginSocketName)};
		if (!IsValid(PrevSocket))
		{
			UE_LOG(LogCarlaDigitalTwinsTool, Warning, TEXT("Previous socket not found at module %d"), i);
			continue;
		}
		if (!IsValid(CurrSocket))
		{
			UE_LOG(LogCarlaDigitalTwinsTool, Warning, TEXT("Current socket not found at module %d"), i);
			continue;
		}

		const FTransform PrevBase{Prev.Transform * Prev.Offset};
		const FTransform PrevLocal(FQuat::Identity, PrevSocket->RelativeLocation, FVector::OneVector);
		const FTransform CurrLocal(FQuat::Identity, CurrSocket->RelativeLocation, FVector::OneVector);
		const FTransform SnapDelta{PrevBase * PrevLocal * CurrLocal.Inverse()};
		Curr.Transform = SnapDelta;
		Curr.ModuleMeshComponent->SetRelativeTransform(Curr.Offset * Curr.Transform);
	}
}

void STrafficLightToolWidget::RefreshPoleList()
{
	if (!RootContainer.IsValid())
	{
		return;
	}
	RootContainer->ClearChildren();

	const int32 NumPoles{FMath::Min(EditorPoles.Num(), Poles.Num())};
	for (int32 PoleIndex{0}; PoleIndex < NumPoles; ++PoleIndex)
	{
		RootContainer->AddSlot().AutoHeight().Padding(2, 2)[BuildPoleEntry(PoleIndex)];
	}
}

void STrafficLightToolWidget::RefreshModuleMeshOptions(int32 PoleIndex, int32 HeadIndex, int32 ModuleIndex)
{
	if (!EditorPoles.IsValidIndex(PoleIndex) || !Poles.IsValidIndex(PoleIndex))
	{
		return;
	}
	FEditorPole& EditorPole{EditorPoles[PoleIndex]};
	FTLPole& Pole{Poles[PoleIndex]};

	if (!EditorPole.Heads.IsValidIndex(HeadIndex) || !Pole.Heads.IsValidIndex(HeadIndex))
	{
		return;
	}
	FEditorHead& EditorHead{EditorPole.Heads[HeadIndex]};
	FTLHead& Head{Pole.Heads[HeadIndex]};

	if (!EditorHead.Modules.IsValidIndex(ModuleIndex) || !Head.Modules.IsValidIndex(ModuleIndex))
	{
		return;
	}
	FEditorModule& EditorModule{EditorHead.Modules[ModuleIndex]};
	FTLModule& Module{Head.Modules[ModuleIndex]};

	TArray<UStaticMesh*> Meshes{FTLMeshFactory::GetAllMeshesForModule(Head, Module)};

	EditorModule.MeshNameOptions.Empty();
	if (IsValid(Module.ModuleMesh))
	{
		if (!Meshes.Contains(Module.ModuleMesh))
		{
			Module.ModuleMesh = nullptr;
		}
	}
}

void STrafficLightToolWidget::RefreshPoleMeshes(int32 PoleIndex)
{
	if (!EditorPoles.IsValidIndex(PoleIndex) || !Poles.IsValidIndex(PoleIndex))
	{
		return;
	}
	FTLPole& Pole{Poles[PoleIndex]};
	TArray<UStaticMesh*> BaseMeshes{FTLMeshFactory::GetAllBaseMeshesForPole(Pole)};
	TArray<UStaticMesh*> ExtensibleMeshes{FTLMeshFactory::GetAllExtensibleMeshesForPole(Pole)};
	TArray<UStaticMesh*> CapMeshes{FTLMeshFactory::GetAllCapMeshesForPole(Pole)};

	if (IsValid(Pole.BasePoleMesh))
	{
		if (!BaseMeshes.Contains(Pole.BasePoleMesh))
		{
			Pole.BasePoleMesh = nullptr;
		}
	}
	if (IsValid(Pole.ExtensiblePoleMesh))
	{
		if (!ExtensibleMeshes.Contains(Pole.ExtensiblePoleMesh))
		{
			Pole.ExtensiblePoleMesh = nullptr;
		}
	}
	if (IsValid(Pole.CapPoleMesh))
	{
		if (!CapMeshes.Contains(Pole.CapPoleMesh))
		{
			Pole.CapPoleMesh = nullptr;
		}
	}
}

// ----------------------------------------------------------------------------------------------------------------
// ******************************   EVENTS
// ----------------------------------------------------------------------------------------------------------------
FReply STrafficLightToolWidget::OnExportJSONClicked()
{
	if (!PreviewViewport.IsValid())
	{
		return FReply::Unhandled();
	}
	ATrafficLightActor* TL{PreviewViewport->GetPreviewTrafficLight()};
	if (!IsValid(TL))
	{
		return FReply::Handled();
	}
	bool bUseTransform{false};
#if WITH_EDITOR
	if (GEditor)
	{
		if (USelection* Sel{GEditor->GetSelectedActors()})
		{
			for (FSelectionIterator It(*Sel); It; ++It)
			{
				if (AActor * Actor{Cast<AActor>(*It)})
				{
					const FString Folder{Actor->GetFolderPath().ToString()};
					if (Folder.StartsWith(TEXT("TrafficLightActor")))
					{
						UE_LOG(LogCarlaDigitalTwinsTool, Log, TEXT("Traffic Light bake folder detected: %s"), *Folder);
						bUseTransform = true;
						break;
					}
				}
			}
		}
	}
#endif

	const FString JsonString{TL->ExportToJSON(bUseTransform)};
	IDesktopPlatform* Desktop{FDesktopPlatformModule::Get()};
	if (Desktop)
	{
		const void* ParentWindowHandle{FSlateApplication::Get().FindBestParentWindowHandleForDialogs(nullptr)};
		TArray<FString> OutFiles;
		const bool bSaved = Desktop->SaveFileDialog(ParentWindowHandle, TEXT("Save Traffic Light JSON"), FPaths::ProjectDir(),
			TEXT("TrafficLight.json"), TEXT("JSON files (*.json)|*.json"), EFileDialogFlags::None, OutFiles);
		if (bSaved && OutFiles.Num() > 0)
		{
			FFileHelper::SaveStringToFile(JsonString, *OutFiles[0]);
		}
	}
	return FReply::Handled();
}

FReply STrafficLightToolWidget::OnImportJSONClicked()
{
	IDesktopPlatform* Desktop{FDesktopPlatformModule::Get()};
	if (!Desktop)
	{
		return FReply::Handled();
	}
	const void* ParentWindowHandle{FSlateApplication::Get().FindBestParentWindowHandleForDialogs(nullptr)};
	TArray<FString> Filenames;
	const bool bFileChosen{Desktop->OpenFileDialog(ParentWindowHandle, TEXT("Import Traffic Light JSON"),
		FPaths::ProjectContentDir(), TEXT(""), TEXT("JSON files (*.json)|*.json"), EFileDialogFlags::None, Filenames)};
	if (!bFileChosen || Filenames.Num() == 0)
	{
		return FReply::Handled();
	}

	const FString& FilePath{Filenames[0]};
	FString JSONString;
	if (!FFileHelper::LoadFileToString(JSONString, *FilePath))
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("Failed to load JSON: %s"), *FilePath);
		return FReply::Handled();
	}

	if (!PreviewViewport.IsValid())
	{
		return FReply::Handled();
	}

	ATrafficLightActor* TL{PreviewViewport->GetPreviewTrafficLight()};
	if (!IsValid(TL))
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("No preview actor available"));
		return FReply::Handled();
	}

	TL->BuildFromJSONString(JSONString);
	Poles = TL->Poles;
	EditorPoles.Reset();
	for (const FTLPole& P : Poles)
	{
		FEditorPole EPol;
		EPol.Heads.SetNum(P.Heads.Num());
		for (int32 h = 0; h < P.Heads.Num(); ++h)
		{
			const FTLHead& H{P.Heads[h]};
			EPol.Heads[h].Modules.SetNum(H.Modules.Num());
			for (int32 m{0}; m < H.Modules.Num(); ++m)
			{
				const FTLModule& M{H.Modules[m]};
				EPol.Heads[h].Modules[m].Lights.SetNum(M.Lights.Num());
			}
		}
		EditorPoles.Add(MoveTemp(EPol));
	}

	Rebuild();

	return FReply::Handled();
}

FReply STrafficLightToolWidget::OnModifyClicked()
{
	if (!PreviewViewport.IsValid())
	{
		return FReply::Handled();
	}

	ATrafficLightActor* const PreviewActor{PreviewViewport->GetPreviewTrafficLight()};
	if (!IsValid(PreviewActor))
	{
		return FReply::Handled();
	}

	if (!GEditor)
	{
		return FReply::Handled();
	}

	UWorld* const EditorWorld{GEditor->GetEditorWorldContext().World()};
	if (!EditorWorld)
	{
		return FReply::Handled();
	}

	int32 SelectedCount{0};
	AActor* SelectedActor{nullptr};
	for (FSelectionIterator It{GEditor->GetSelectedActorIterator()}; It; ++It)
	{
		AActor* const Actor{Cast<AActor>(*It)};
		if (IsValid(Actor))
		{
			SelectedActor = Actor;
			++SelectedCount;
		}
	}

	if (SelectedCount != 1)
	{
		return FReply::Handled();
	}

	USceneComponent* const SelectedRoot{SelectedActor->GetRootComponent()};
	if (!(IsValid(SelectedRoot) && SelectedRoot->GetName() == TEXT("BakedRoot")))
	{
		return FReply::Handled();
	}

	const FTransform SelectedTransform{SelectedActor->GetActorTransform()};
	const FString DesiredLabel{SelectedActor->GetActorLabel()};
	const FName OriginalFolderPath{SelectedActor->GetFolderPath()};

	FString MapName{};
	{
		const FName MountPoint{FPackageName::GetPackageMountPoint(EditorWorld->GetOutermost()->GetName())};
		const FString MountPointStr{MountPoint.ToString()};
		if (!MountPoint.IsNone() && MountPointStr != TEXT("Game") && MountPointStr != TEXT("Engine") &&
			MountPointStr != TEXT("CarlaDigitalTwinsTool") &&
			FPaths::DirectoryExists(FPaths::ProjectPluginsDir() / MountPointStr))
		{
			MapName = MountPointStr;
		}
		else
		{
			FString CurrentMapName{EditorWorld->GetMapName()};
			CurrentMapName.RemoveFromStart(EditorWorld->StreamingLevelsPrefix);
			MapName = CurrentMapName;
		}
	}

	const FString BpPath{TEXT("/CarlaDigitalTwinsTool/Blueprints/TrafficLight/BP_TrafficLightActor.BP_TrafficLightActor_C")};
	UClass* const TrafficLightBpClass{LoadObject<UClass>(nullptr, *BpPath)};
	if (!IsValid(TrafficLightBpClass))
	{
		return FReply::Handled();
	}

	const FScopedTransaction Transaction{NSLOCTEXT("TLW", "ModifyTrafficLight", "Modify Traffic Light")};

	ATrafficLightActor* const NewTrafficLightActor{EditorWorld->SpawnActorDeferred<ATrafficLightActor>(
		TrafficLightBpClass, SelectedTransform, nullptr, nullptr, ESpawnActorCollisionHandlingMethod::AlwaysSpawn)};
	if (!IsValid(NewTrafficLightActor))
	{
		return FReply::Handled();
	}

	AActor* const ActorToDestroy{SelectedActor};
	SelectedActor = nullptr;
	if (IsValid(ActorToDestroy))
	{
		ActorToDestroy->Modify();
		ActorToDestroy->Destroy();
	}

	NewTrafficLightActor->Poles = PreviewActor->Poles;
	NewTrafficLightActor->JunctionID = PreviewActor->JunctionID;
	NewTrafficLightActor->SignalID = PreviewActor->SignalID;
	NewTrafficLightActor->TrafficLightGroupID = PreviewActor->TrafficLightGroupID;
	NewTrafficLightActor->RedDuration = PreviewActor->RedDuration;
	NewTrafficLightActor->GreenDuration = PreviewActor->GreenDuration;
	NewTrafficLightActor->AmberDuration = PreviewActor->AmberDuration;
	NewTrafficLightActor->AmberBlinkInterval = PreviewActor->AmberBlinkInterval;

	// Preserve existing OpenDRIVE logic metadata from map_logic.json when PreviewActor has none set
	if (NewTrafficLightActor->SignalID.IsEmpty() && !MapName.IsEmpty() && !MapName.Contains(TEXT("..")))
	{
		const FString MapLogicFilePath =
			FPaths::ProjectPluginsDir() / MapName / TEXT("Content/Maps/OpenDrive/map_logic.json");
		FString ExistingContent;
		if (FFileHelper::LoadFileToString(ExistingContent, *MapLogicFilePath))
		{
			TSharedPtr<FJsonObject> MapLogicRoot;
			TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(ExistingContent);
			const TArray<TSharedPtr<FJsonValue>>* ExistingTrafficLights = nullptr;
			if (FJsonSerializer::Deserialize(Reader, MapLogicRoot) && MapLogicRoot.IsValid() &&
				MapLogicRoot->TryGetArrayField(TEXT("TrafficLights"), ExistingTrafficLights) && ExistingTrafficLights)
			{
				for (const TSharedPtr<FJsonValue>& EntryVal : *ExistingTrafficLights)
				{
					const TSharedPtr<FJsonObject> EntryObj = EntryVal.IsValid() ? EntryVal->AsObject() : nullptr;
					FString EntryActorName;
					if (!EntryObj.IsValid() || !EntryObj->TryGetStringField(TEXT("ActorName"), EntryActorName) ||
						EntryActorName != DesiredLabel)
					{
						continue;
					}

					EntryObj->TryGetStringField(TEXT("SignalID"), NewTrafficLightActor->SignalID);
					if (NewTrafficLightActor->JunctionID == MIN_int32)
					{
						int32 ExistingJunctionId = MIN_int32;
						if (EntryObj->TryGetNumberField(TEXT("JunctionID"), ExistingJunctionId))
						{
							NewTrafficLightActor->JunctionID = ExistingJunctionId;
						}
					}
					if (NewTrafficLightActor->TrafficLightGroupID.IsEmpty())
					{
						EntryObj->TryGetStringField(TEXT("TrafficLightGroupID"), NewTrafficLightActor->TrafficLightGroupID);
					}

					const TSharedPtr<FJsonObject>* TimingObj = nullptr;
					if (EntryObj->TryGetObjectField(TEXT("Timing"), TimingObj) && TimingObj && TimingObj->IsValid())
					{
						double Val = 0.0;
						if ((*TimingObj)->TryGetNumberField(TEXT("RedDuration"), Val))
						{
							NewTrafficLightActor->RedDuration = static_cast<float>(Val);
						}
						if ((*TimingObj)->TryGetNumberField(TEXT("GreenDuration"), Val))
						{
							NewTrafficLightActor->GreenDuration = static_cast<float>(Val);
						}
						if ((*TimingObj)->TryGetNumberField(TEXT("AmberDuration"), Val))
						{
							NewTrafficLightActor->AmberDuration = static_cast<float>(Val);
						}
						if ((*TimingObj)->TryGetNumberField(TEXT("AmberBlinkInterval"), Val))
						{
							NewTrafficLightActor->AmberBlinkInterval = static_cast<float>(Val);
						}
					}

					bool bHasAnyLaneIds = false;
					FTLModule* FirstModule = nullptr;
					for (FTLPole& Pole : NewTrafficLightActor->Poles)
					{
						for (FTLHead& Head : Pole.Heads)
						{
							for (FTLModule& Mod : Head.Modules)
							{
								if (!FirstModule)
								{
									FirstModule = &Mod;
								}
								if (!Mod.LaneIds.IsEmpty())
								{
									bHasAnyLaneIds = true;
								}
							}
						}
					}
					const TArray<TSharedPtr<FJsonValue>>* ExistingHeads = nullptr;
					if (EntryObj->TryGetArrayField(TEXT("Heads"), ExistingHeads) && ExistingHeads)
					{
						int32 FlatHeadIdx = 0;
						for (FTLPole& Pole : NewTrafficLightActor->Poles)
						{
							for (FTLHead& Head : Pole.Heads)
							{
								if (ExistingHeads->IsValidIndex(FlatHeadIdx))
								{
									const TSharedPtr<FJsonObject> HeadEntryObj =
										(*ExistingHeads)[FlatHeadIdx].IsValid() ? (*ExistingHeads)[FlatHeadIdx]->AsObject() : nullptr;
									if (HeadEntryObj.IsValid())
									{
										FString HeadSig;
										if (HeadEntryObj->TryGetStringField(TEXT("SignalID"), HeadSig) &&
											HeadSig != NewTrafficLightActor->SignalID)
										{
											Head.SignalID = HeadSig;
										}
										const TArray<TSharedPtr<FJsonValue>>* HeadLaneArr = nullptr;
										if (!Head.Modules.IsEmpty() &&
											HeadEntryObj->TryGetArrayField(TEXT("LaneIds"), HeadLaneArr) && HeadLaneArr)
										{
											for (const TSharedPtr<FJsonValue>& LaneVal : *HeadLaneArr)
											{
												int32 LaneId = 0;
												if (LaneVal.IsValid() && LaneVal->TryGetNumber(LaneId))
												{
													Head.Modules[0].LaneIds.AddUnique(LaneId);
													bHasAnyLaneIds = true;
												}
											}
										}
									}
								}
								++FlatHeadIdx;
							}
						}
					}
					const TArray<TSharedPtr<FJsonValue>>* ExistingModules = nullptr;
					if (!bHasAnyLaneIds && FirstModule && EntryObj->TryGetArrayField(TEXT("Modules"), ExistingModules) &&
						ExistingModules)
					{
						for (const TSharedPtr<FJsonValue>& ModVal : *ExistingModules)
						{
							const TSharedPtr<FJsonObject> ModObj = ModVal.IsValid() ? ModVal->AsObject() : nullptr;
							const TArray<TSharedPtr<FJsonValue>>* LaneArr = nullptr;
							if (ModObj.IsValid() && ModObj->TryGetArrayField(TEXT("LaneIds"), LaneArr) && LaneArr)
							{
								for (const TSharedPtr<FJsonValue>& LaneVal : *LaneArr)
								{
									int32 LaneId = 0;
									if (LaneVal.IsValid() && LaneVal->TryGetNumber(LaneId))
									{
										FirstModule->LaneIds.AddUnique(LaneId);
									}
								}
							}
						}
					}
					break;
				}
			}
		}
	}

	NewTrafficLightActor->FinishSpawning(SelectedTransform);
	NewTrafficLightActor->SetActorTransform(SelectedTransform, false, nullptr, ETeleportType::TeleportPhysics);
	NewTrafficLightActor->Build();
	NewTrafficLightActor->Bake(MapName, DesiredLabel);
	NewTrafficLightActor->Destroy();

	AActor* BakedActorFound{nullptr};
	for (TActorIterator<AActor> It{EditorWorld}; It; ++It)
	{
		AActor* const Actor{*It};
		if (!IsValid(Actor))
		{
			continue;
		}
		if (Actor->GetActorLabel() != DesiredLabel)
		{
			continue;
		}
		USceneComponent* const Root{Actor->GetRootComponent()};
		if (!(IsValid(Root) && Root->GetName() == TEXT("BakedRoot")))
		{
			continue;
		}
		BakedActorFound = Actor;
		break;
	}

	if (IsValid(BakedActorFound) && !OriginalFolderPath.IsNone())
	{
		BakedActorFound->SetFolderPath(OriginalFolderPath);
	}

	GEditor->SelectNone(false, true);
	if (IsValid(BakedActorFound))
	{
		GEditor->SelectActor(BakedActorFound, true, true);
	}
	GEditor->NoteSelectionChange();

	return FReply::Handled();
}

FReply STrafficLightToolWidget::OnAddPoleClicked()
{
	const int32 PoleIndex{EditorPoles.Emplace()};
	Poles.Emplace();

	Rebuild();
	if (PoleIndex == 0 && PreviewViewport.IsValid())
	{
		PreviewViewport->ResetFrame();
		PreviewViewport->Reload();
	}
	return FReply::Handled();
}

FReply STrafficLightToolWidget::OnDeletePoleClicked(int32 PoleIndex)
{
	if (!EditorPoles.IsValidIndex(PoleIndex) || !Poles.IsValidIndex(PoleIndex))
	{
		return FReply::Handled();
	}
	EditorPoles.RemoveAt(PoleIndex);
	Poles.RemoveAt(PoleIndex);
	Rebuild();

	return FReply::Handled();
}

FReply STrafficLightToolWidget::OnAddHeadClicked(int32 PoleIndex)
{
	if (!EditorPoles.IsValidIndex(PoleIndex) || !Poles.IsValidIndex(PoleIndex))
	{
		return FReply::Handled();
	}
	FEditorPole& EditorPole{EditorPoles[PoleIndex]};
	FTLPole& Pole{Poles[PoleIndex]};

	const int32 HeadIndex{EditorPole.Heads.Emplace()};
	Pole.Heads.Emplace();

	OnAddModuleClicked(PoleIndex, HeadIndex);
	if (Pole.Heads[HeadIndex].Modules.IsEmpty())
	{
		Rebuild();
	}

	return FReply::Handled();
}

FReply STrafficLightToolWidget::OnDeleteHeadClicked(int32 PoleIndex, int32 HeadIndex)
{
	if (!EditorPoles.IsValidIndex(PoleIndex) || !Poles.IsValidIndex(PoleIndex))
	{
		return FReply::Handled();
	}
	FEditorPole& EditorPole{EditorPoles[PoleIndex]};
	FTLPole& Pole{Poles[PoleIndex]};

	if (!EditorPole.Heads.IsValidIndex(HeadIndex) || !Pole.Heads.IsValidIndex(HeadIndex))
	{
		return FReply::Handled();
	}
	EditorPole.Heads.RemoveAt(HeadIndex);
	Pole.Heads.RemoveAt(HeadIndex);
	Rebuild();

	return FReply::Handled();
}

FReply STrafficLightToolWidget::OnAddModuleClicked(int32 PoleIndex, int32 HeadIndex)
{
	if (!EditorPoles.IsValidIndex(PoleIndex) || !Poles.IsValidIndex(PoleIndex))
	{
		return FReply::Handled();
	}
	FEditorPole& EditorPole{EditorPoles[PoleIndex]};
	FTLPole& Pole{Poles[PoleIndex]};

	if (!EditorPole.Heads.IsValidIndex(HeadIndex) || !Pole.Heads.IsValidIndex(HeadIndex))
	{
		return FReply::Handled();
	}
	FEditorHead& EditorHead{EditorPole.Heads[HeadIndex]};
	FTLHead& Head{Pole.Heads[HeadIndex]};

	FEditorModule NewEditorModule;
	FTLModule NewModule;

	NewModule.ModuleMesh = FTLMeshFactory::GetMeshForModule(Head, NewModule);
	if (!IsValid(NewModule.ModuleMesh))
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("OnAddModuleClicked: failed to load the module."));
		return FReply::Handled();
	}

	SyncModuleLightsWithMesh(NewModule, NewEditorModule);

	Head.Modules.Add(MoveTemp(NewModule));
	EditorHead.Modules.Add(MoveTemp(NewEditorModule));
	Rebuild();

	return FReply::Handled();
}

FReply STrafficLightToolWidget::OnDeleteModuleClicked(int32 PoleIndex, int32 HeadIndex, int32 ModuleIndex)
{
	if (!EditorPoles.IsValidIndex(PoleIndex) || !Poles.IsValidIndex(PoleIndex))
	{
		return FReply::Handled();
	}
	FEditorPole& EditorPole{EditorPoles[PoleIndex]};
	FTLPole& Pole{Poles[PoleIndex]};

	if (!EditorPole.Heads.IsValidIndex(HeadIndex) || !Pole.Heads.IsValidIndex(HeadIndex))
	{
		return FReply::Handled();
	}
	FEditorHead& EditorHead{EditorPole.Heads[HeadIndex]};
	FTLHead& Head{Pole.Heads[HeadIndex]};

	if (!EditorHead.Modules.IsValidIndex(ModuleIndex) || !Head.Modules.IsValidIndex(ModuleIndex))
	{
		return FReply::Handled();
	}
	EditorHead.Modules.RemoveAt(ModuleIndex);
	Head.Modules.RemoveAt(ModuleIndex);
	Rebuild();

	return FReply::Handled();
}

FReply STrafficLightToolWidget::OnMoveModuleUpClicked(int32 PoleIndex, int32 HeadIndex, int32 ModuleIndex)
{
	if (!EditorPoles.IsValidIndex(PoleIndex) || !Poles.IsValidIndex(PoleIndex))
	{
		return FReply::Handled();
	}
	FEditorPole& EditorPole{EditorPoles[PoleIndex]};
	FTLPole& Pole{Poles[PoleIndex]};

	if (!EditorPole.Heads.IsValidIndex(HeadIndex) || !Pole.Heads.IsValidIndex(HeadIndex))
	{
		return FReply::Handled();
	}
	FEditorHead& EditorHead{EditorPole.Heads[HeadIndex]};
	FTLHead& Head{Pole.Heads[HeadIndex]};

	if (ModuleIndex > 0 && ModuleIndex < EditorHead.Modules.Num() && ModuleIndex < Head.Modules.Num())
	{
		EditorHead.Modules.Swap(ModuleIndex, ModuleIndex - 1);
		Head.Modules.Swap(ModuleIndex, ModuleIndex - 1);
		Rebuild();
	}

	return FReply::Handled();
}

FReply STrafficLightToolWidget::OnMoveModuleDownClicked(int32 PoleIndex, int32 HeadIndex, int32 ModuleIndex)
{
	if (!EditorPoles.IsValidIndex(PoleIndex) || !Poles.IsValidIndex(PoleIndex))
	{
		return FReply::Handled();
	}
	FEditorPole& EditorPole{EditorPoles[PoleIndex]};
	FTLPole& Pole{Poles[PoleIndex]};

	if (!EditorPole.Heads.IsValidIndex(HeadIndex) || !Pole.Heads.IsValidIndex(HeadIndex))
	{
		return FReply::Handled();
	}
	FEditorHead& EditorHead{EditorPole.Heads[HeadIndex]};
	FTLHead& Head{Pole.Heads[HeadIndex]};

	if (ModuleIndex >= 0 && ModuleIndex < EditorHead.Modules.Num() - 1 && ModuleIndex < Head.Modules.Num() - 1)
	{
		EditorHead.Modules.Swap(ModuleIndex, ModuleIndex + 1);
		Head.Modules.Swap(ModuleIndex, ModuleIndex + 1);
		Rebuild();
	}

	return FReply::Handled();
}

void STrafficLightToolWidget::OnHeadOrientationChanged(ETLOrientation NewOrientation, int32 PoleIndex, int32 HeadIndex)
{
	if (!EditorPoles.IsValidIndex(PoleIndex) || !Poles.IsValidIndex(PoleIndex))
	{
		return;
	}
	FTLPole& Pole{Poles[PoleIndex]};
	FEditorPole& EditorPole{EditorPoles[PoleIndex]};
	if (!Pole.Heads.IsValidIndex(HeadIndex) || !EditorPole.Heads.IsValidIndex(HeadIndex))
	{
		return;
	}
	FTLHead& Head{Pole.Heads[HeadIndex]};
	FEditorHead& EditorHead{EditorPole.Heads[HeadIndex]};
	Head.Orientation = NewOrientation;
	const int32 NumModules{FMath::Min(Head.Modules.Num(), EditorHead.Modules.Num())};
	for (int32 ModuleIndex{0}; ModuleIndex < NumModules; ++ModuleIndex)
	{
		FTLModule& Module{Head.Modules[ModuleIndex]};
		RefreshModuleMeshOptions(PoleIndex, HeadIndex, ModuleIndex);
		if (!IsValid(Module.ModuleMesh))
		{
			Module.ModuleMesh = FTLMeshFactory::GetMeshForModule(Head, Module);
		}
		SyncModuleLightsWithMesh(Module, EditorHead.Modules[ModuleIndex]);
	}
	Rebuild();
}

void STrafficLightToolWidget::OnHeadStyleChanged(ETLStyle NewStyle, int32 PoleIndex, int32 HeadIndex)
{
	if (!EditorPoles.IsValidIndex(PoleIndex) || !Poles.IsValidIndex(PoleIndex))
	{
		return;
	}
	FTLPole& Pole{Poles[PoleIndex]};
	FEditorPole& EditorPole{EditorPoles[PoleIndex]};
	if (!Pole.Heads.IsValidIndex(HeadIndex) || !EditorPole.Heads.IsValidIndex(HeadIndex))
	{
		return;
	}
	FTLHead& Head{Pole.Heads[HeadIndex]};
	FEditorHead& EditorHead{EditorPole.Heads[HeadIndex]};
	Head.Style = NewStyle;
	const int32 NumModules{FMath::Min(Head.Modules.Num(), EditorHead.Modules.Num())};
	for (int32 ModuleIndex{0}; ModuleIndex < NumModules; ++ModuleIndex)
	{
		FTLModule& Module{Head.Modules[ModuleIndex]};
		RefreshModuleMeshOptions(PoleIndex, HeadIndex, ModuleIndex);
		if (!IsValid(Module.ModuleMesh))
		{
			Module.ModuleMesh = FTLMeshFactory::GetMeshForModule(Head, Module);
		}
		SyncModuleLightsWithMesh(Module, EditorHead.Modules[ModuleIndex]);
	}
	Rebuild();
}

void STrafficLightToolWidget::OnPoleOrientationChanged(ETLOrientation NewOrientation, int32 PoleIndex)
{
	if (!EditorPoles.IsValidIndex(PoleIndex) || !Poles.IsValidIndex(PoleIndex))
	{
		return;
	}
	FTLPole& Pole{Poles[PoleIndex]};
	Pole.Orientation = NewOrientation;
	RefreshPoleMeshes(PoleIndex);
	Rebuild();
}

void STrafficLightToolWidget::OnPoleStyleChanged(ETLStyle NewStyle, int32 PoleIndex)
{
	if (!EditorPoles.IsValidIndex(PoleIndex) || !Poles.IsValidIndex(PoleIndex))
	{
		return;
	}
	FTLPole& Pole{Poles[PoleIndex]};
	Pole.Style = NewStyle;
	RefreshPoleMeshes(PoleIndex);
	Rebuild();
}

void STrafficLightToolWidget::OnModuleVisorChanged(ECheckBoxState NewState, int32 PoleIndex, int32 HeadIndex, int32 ModuleIndex)
{
	if (!EditorPoles.IsValidIndex(PoleIndex) || !Poles.IsValidIndex(PoleIndex) ||
		!EditorPoles[PoleIndex].Heads.IsValidIndex(HeadIndex) || !Poles[PoleIndex].Heads.IsValidIndex(HeadIndex) ||
		!EditorPoles[PoleIndex].Heads[HeadIndex].Modules.IsValidIndex(ModuleIndex) ||
		!Poles[PoleIndex].Heads[HeadIndex].Modules.IsValidIndex(ModuleIndex))
	{
		return;
	}
	FTLPole& Pole{Poles[PoleIndex]};
	FTLHead& Head{Pole.Heads[HeadIndex]};
	FTLModule& Module{Head.Modules[ModuleIndex]};

	Module.bHasVisor = (NewState == ECheckBoxState::Checked);
	UStaticMesh* NewStaticMesh{FTLMeshFactory::GetMeshForModule(Head, Module)};
	if (!IsValid(NewStaticMesh))
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("OnModuleVisorChanged: failed to get mesh"));
		return;
	}
	if (Module.ModuleMesh != NewStaticMesh)
	{
		Module.ModuleMesh = NewStaticMesh;
		SyncModuleLightsWithMesh(Module, EditorPoles[PoleIndex].Heads[HeadIndex].Modules[ModuleIndex]);
	}

	Rebuild();
}

// ----------------------------------------------------------------------------------------------------------------
// ******************************   BUILD SECTIONS
// ----------------------------------------------------------------------------------------------------------------
TSharedRef<SWidget> STrafficLightToolWidget::BuildPoleEntry(int32 PoleIndex)
{
	if (!EditorPoles.IsValidIndex(PoleIndex) || !Poles.IsValidIndex(PoleIndex))
	{
		return SNullWidget::NullWidget;
	}
	FEditorPole& EditorPole{EditorPoles[PoleIndex]};
	FTLPole& Pole{Poles[PoleIndex]};

	TArray<TSharedPtr<FString>>& BaseNames{EditorPole.BaseMeshNameOptions};
	TArray<UStaticMesh*> BasePoleMeshes{FTLMeshFactory::GetAllBaseMeshesForPole(Pole)};
	BaseNames.Empty();
	for (const UStaticMesh* Mesh : BasePoleMeshes)
	{
		if (IsValid(Mesh))
		{
			BaseNames.Add(MakeShared<FString>(Mesh->GetName()));
		}
	}

	TArray<TSharedPtr<FString>>& ExtensibleNames{EditorPole.ExtensibleMeshNameOptions};
	TArray<UStaticMesh*> ExtensiblePoleMeshes{FTLMeshFactory::GetAllExtensibleMeshesForPole(Pole)};
	ExtensibleNames.Empty();
	for (const UStaticMesh* Mesh : ExtensiblePoleMeshes)
	{
		if (IsValid(Mesh))
		{
			ExtensibleNames.Add(MakeShared<FString>(Mesh->GetName()));
		}
	}

	TArray<TSharedPtr<FString>>& CapNames{EditorPole.CapMeshNameOptions};
	TArray<UStaticMesh*> CapPoleMeshes{FTLMeshFactory::GetAllCapMeshesForPole(Pole)};
	CapNames.Empty();
	for (const UStaticMesh* Mesh : CapPoleMeshes)
	{
		if (IsValid(Mesh))
		{
			CapNames.Add(MakeShared<FString>(Mesh->GetName()));
		}
	}

	const int32 InitialStyleIdx{static_cast<int32>(Pole.Style)};
	const TSharedPtr<FString> InitialStyleItem{
		StyleOptions.IsValidIndex(InitialStyleIdx) ? StyleOptions[InitialStyleIdx] : TSharedPtr<FString>()};
	const int32 InitialOrientIdx{static_cast<int32>(Pole.Orientation)};
	const TSharedPtr<FString> InitialOrientItem{
		OrientationOptions.IsValidIndex(InitialOrientIdx) ? OrientationOptions[InitialOrientIdx] : TSharedPtr<FString>()};

	// clang-format off
	return SNew(SExpandableArea)
		.InitiallyCollapsed(!EditorPole.bExpanded)
		.AreaTitle(FText::FromString(FString::Printf(TEXT("Pole #%d"), PoleIndex)))
		.Padding(4)
		.OnAreaExpansionChanged_Lambda([this, PoleIndex](bool b)
		{
			if (EditorPoles.IsValidIndex(PoleIndex))
			{
				EditorPoles[PoleIndex].bExpanded = b;
			}
		})
		.BodyContent()
		[SNew(SVerticalBox)
			// — STYLE ComboBox —
			+ SVerticalBox::Slot()
			.AutoHeight()
			.Padding(2, 4)
			[SNew(SHorizontalBox) +
				SHorizontalBox::Slot()
				.AutoWidth()
				.VAlign(VAlign_Center)
				[SNew(STextBlock)
					.Text(FText::FromString("Style:"))
				] +
				SHorizontalBox::Slot()
				.AutoWidth()
				.Padding(8,0)
				[SNew(SComboBox<TSharedPtr<FString>>)
					.OptionsSource(&StyleOptions)
					.InitiallySelectedItem(InitialStyleItem)
					.OnGenerateWidget_Lambda([](TSharedPtr<FString> In)
					{
						return SNew(STextBlock).Text(FText::FromString(In.IsValid() ? *In : FString()));
					})
					.OnSelectionChanged_Lambda([this, PoleIndex](TSharedPtr<FString> NewStyle, ESelectInfo::Type)
					{
						if (!NewStyle.IsValid() || !Poles.IsValidIndex(PoleIndex))
						{
							return;
						}
						const int32 Choice{StyleOptions.IndexOfByPredicate([&](const auto& S) { return S == NewStyle; })};
						if (Choice == INDEX_NONE)
						{
							return;
						}
						OnPoleStyleChanged(static_cast<ETLStyle>(Choice), PoleIndex);
					})
					[SNew(STextBlock)
						.Text_Lambda([this, PoleIndex]()
						{
							if (!Poles.IsValidIndex(PoleIndex))
							{
								return FText::GetEmpty();
							}
							const int32 Idx{static_cast<int32>(Poles[PoleIndex].Style)};
							return FText::FromString(
								(StyleOptions.IsValidIndex(Idx) && StyleOptions[Idx].IsValid()) ? *StyleOptions[Idx] : FString());
						})
					]
				]
			]
			// — ORIENTATION ComboBox —
			+ SVerticalBox::Slot()
			.AutoHeight()
			.Padding(2, 4)
			[SNew(SHorizontalBox) +
				SHorizontalBox::Slot()
				.AutoWidth()
				.VAlign(VAlign_Center)
				[SNew(STextBlock).
					Text(FText::FromString("Orientation:"))
				] +
				SHorizontalBox::Slot()
				.AutoWidth()
				.Padding(8, 0)
				[SNew(SComboBox<TSharedPtr<FString>>)
					.OptionsSource(&OrientationOptions)
					.InitiallySelectedItem(InitialOrientItem)
					.OnGenerateWidget_Lambda([](TSharedPtr<FString> In)
					{
						return SNew(STextBlock).Text(FText::FromString(In.IsValid() ? *In : FString()));
					})
					.OnSelectionChanged_Lambda([this, PoleIndex](TSharedPtr<FString> NewOrientation, ESelectInfo::Type)
					{
						if (!NewOrientation.IsValid() || !Poles.IsValidIndex(PoleIndex))
						{
							return;
						}
						const int32 Choice{OrientationOptions.IndexOfByPredicate([&](const auto& S) { return S == NewOrientation; })};
						if (Choice == INDEX_NONE)
						{
							return;
						}
						OnPoleOrientationChanged(static_cast<ETLOrientation>(Choice), PoleIndex);
					})
					[SNew(STextBlock)
						.Text_Lambda([this, PoleIndex]()
						{
							if (!Poles.IsValidIndex(PoleIndex))
							{
								return FText::GetEmpty();
							}
							const int32 Idx{static_cast<int32>(Poles[PoleIndex].Orientation)};
							return FText::FromString((OrientationOptions.IsValidIndex(Idx) && OrientationOptions[Idx].IsValid())
									? *OrientationOptions[Idx]
									: FString());
						})
					]
				]
			]
			// — TRANSFORM (Location XYZ) —
			+ SVerticalBox::Slot()
			.AutoHeight()
			.Padding(2, 4)
			[SNew(STextBlock)
				.Text(FText::FromString("Transform Location"))
			] +
			SVerticalBox::Slot()
			.AutoHeight()
			.Padding(2, 0)
			[SNew(SHorizontalBox) +
				SHorizontalBox::Slot()
				.FillWidth(1)
				.Padding(2, 0)
				[SNew(SNumericEntryBox<float>)
					.Value_Lambda([this, PoleIndex]() -> TOptional<float>
					{
						return Poles.IsValidIndex(PoleIndex) ? static_cast<float>(Poles[PoleIndex].Transform.GetLocation().X) : 0.0f;
					})
					.OnValueCommitted_Lambda([this, PoleIndex](float V, ETextCommit::Type)
					{
						if (!Poles.IsValidIndex(PoleIndex))
						{
							return;
						}
						FTLPole& Pole{Poles[PoleIndex]};
						FVector L{Pole.Transform.GetLocation()};
						L.X = V;
						Pole.Transform.SetLocation(L);
						Rebuild();
					})
				] +
				SHorizontalBox::Slot()
				.FillWidth(1)
				.Padding(2, 0)
				[SNew(SNumericEntryBox<float>)
					.Value_Lambda([this, PoleIndex]() -> TOptional<float>
					{
						return Poles.IsValidIndex(PoleIndex) ? static_cast<float>(Poles[PoleIndex].Transform.GetLocation().Y) : 0.0f;
					})
					.OnValueCommitted_Lambda([this, PoleIndex](float V, ETextCommit::Type)
					{
						if (!Poles.IsValidIndex(PoleIndex))
						{
							return;
						}
						FTLPole& Pole{Poles[PoleIndex]};
						FVector L{Pole.Transform.GetLocation()};
						L.Y = V;
						Pole.Transform.SetLocation(L);
						Rebuild();
					})
				] +
				SHorizontalBox::Slot()
				.FillWidth(1)
				.Padding(2, 0)
				[SNew(SNumericEntryBox<float>)
					.Value_Lambda([this, PoleIndex]() -> TOptional<float>
					{
						return Poles.IsValidIndex(PoleIndex) ? static_cast<float>(Poles[PoleIndex].Transform.GetLocation().Z) : 0.0f;
					})
					.OnValueCommitted_Lambda([this, PoleIndex](float V, ETextCommit::Type)
					{
						if (!Poles.IsValidIndex(PoleIndex))
						{
							return;
						}
						FTLPole& Pole{Poles[PoleIndex]};
						FVector L{Pole.Transform.GetLocation()};
						L.Z = V;
						Pole.Transform.SetLocation(L);
						Rebuild();
					})
				]
			]
			// — TRANSFORM (Rotation Pitch/Yaw/Roll) —
			+ SVerticalBox::Slot()
			.AutoHeight()
			.Padding(2, 4)
			[SNew(STextBlock)
				.Text(FText::FromString("Transform Rotation"))
			] +
			SVerticalBox::Slot()
			.AutoHeight()
			.Padding(2, 0)
			[SNew(SHorizontalBox) +
				SHorizontalBox::Slot()
				.FillWidth(1)
				.Padding(2, 0)
				[SNew(SNumericEntryBox<float>)
					.Value_Lambda([this, PoleIndex]() -> TOptional<float>
					{
						return Poles.IsValidIndex(PoleIndex) ? static_cast<float>(Poles[PoleIndex].Transform.Rotator().Pitch) : 0.0f;
					})
					.OnValueCommitted_Lambda([this, PoleIndex](float V, ETextCommit::Type)
					{
						if (!Poles.IsValidIndex(PoleIndex))
						{
							return;
						}
						FTLPole& Pole{Poles[PoleIndex]};
						FRotator R{Pole.Transform.Rotator()};
						R.Pitch = V;
						Pole.Transform.SetRotation(FQuat(R));
						Rebuild();
					})
				] +
				SHorizontalBox::Slot()
				.FillWidth(1)
				.Padding(2, 0)
				[SNew(SNumericEntryBox<float>)
					.Value_Lambda([this, PoleIndex]() -> TOptional<float>
					{
						return Poles.IsValidIndex(PoleIndex) ? static_cast<float>(Poles[PoleIndex].Transform.Rotator().Yaw) : 0.0f;
					})
					.OnValueCommitted_Lambda([this, PoleIndex](float V, ETextCommit::Type)
					{
						if (!Poles.IsValidIndex(PoleIndex))
						{
							return;
						}
						FTLPole& Pole{Poles[PoleIndex]};
						FRotator R{Pole.Transform.Rotator()};
						R.Yaw = V;
						Pole.Transform.SetRotation(FQuat(R));
						Rebuild();
					})
				] +
				SHorizontalBox::Slot()
				.FillWidth(1)
				.Padding(2, 0)
				[SNew(SNumericEntryBox<float>)
					.Value_Lambda([this, PoleIndex]() -> TOptional<float>
					{
						return Poles.IsValidIndex(PoleIndex) ? static_cast<float>(Poles[PoleIndex].Transform.Rotator().Roll) : 0.0f;
					})
					.OnValueCommitted_Lambda([this, PoleIndex](float V, ETextCommit::Type)
					{
						if (!Poles.IsValidIndex(PoleIndex))
						{
							return;
						}
						FTLPole& Pole{Poles[PoleIndex]};
						FRotator R{Pole.Transform.Rotator()};
						R.Roll = V;
						Pole.Transform.SetRotation(FQuat(R));
						Rebuild();
					})
				]
			]

			// — TRANSFORM (Scale XYZ) —
			+ SVerticalBox::Slot()
			.AutoHeight()
			.Padding(2, 4)
			[SNew(STextBlock)
				.Text(FText::FromString("Transform Scale"))
			] +
			SVerticalBox::Slot()
			.AutoHeight()
			.Padding(2, 0)
			[SNew(SHorizontalBox) +
				SHorizontalBox::Slot()
				.FillWidth(1)
				.Padding(2, 0)
				[SNew(SNumericEntryBox<float>)
					.Value_Lambda([this, PoleIndex]() -> TOptional<float>
					{
						return Poles.IsValidIndex(PoleIndex) ? static_cast<float>(Poles[PoleIndex].Transform.GetScale3D().X) : 1.0f;
					})
					.OnValueCommitted_Lambda([this, PoleIndex](float V, ETextCommit::Type)
					{
						if (!Poles.IsValidIndex(PoleIndex))
						{
							return;
						}
						FTLPole& Pole{Poles[PoleIndex]};
						FVector S{Pole.Transform.GetScale3D()};
						S.X = V;
						Pole.Transform.SetScale3D(S);
						Rebuild();
					})
				] +
				SHorizontalBox::Slot()
				.FillWidth(1)
				.Padding(2, 0)
				[SNew(SNumericEntryBox<float>)
					.Value_Lambda([this, PoleIndex]() -> TOptional<float>
					{
						return Poles.IsValidIndex(PoleIndex) ? static_cast<float>(Poles[PoleIndex].Transform.GetScale3D().Y) : 1.0f;
					})
					.OnValueCommitted_Lambda([this, PoleIndex](float V, ETextCommit::Type)
					{
						if (!Poles.IsValidIndex(PoleIndex))
						{
							return;
						}
						FTLPole& Pole{Poles[PoleIndex]};
						FVector S{Pole.Transform.GetScale3D()};
						S.Y = V;
						Pole.Transform.SetScale3D(S);
						Rebuild();
					})
				] +
				SHorizontalBox::Slot()
				.FillWidth(1)
				.Padding(2, 0)
				[SNew(SNumericEntryBox<float>)
					.Value_Lambda(
						[this, PoleIndex]() -> TOptional<float>
						{
							return Poles.IsValidIndex(PoleIndex) ? static_cast<float>(Poles[PoleIndex].Transform.GetScale3D().Z) : 1.0f;
						})
					.OnValueCommitted_Lambda([this, PoleIndex](float V, ETextCommit::Type)
					{
						if (!Poles.IsValidIndex(PoleIndex))
						{
							return;
						}
						FTLPole& Pole{Poles[PoleIndex]};
						FVector S{Pole.Transform.GetScale3D()};
						S.Z = V;
						Pole.Transform.SetScale3D(S);
						Rebuild();
					})
				]
			]

			// — Base Mesh Combo —
			+ SVerticalBox::Slot()
			.AutoHeight()
			.Padding(2, 4)
			[SNew(SHorizontalBox) +
				SHorizontalBox::Slot()
				.AutoWidth()
				.VAlign(VAlign_Center)
				[SNew(STextBlock)
					.Text(FText::FromString("Base Mesh:"))
				] +
				SHorizontalBox::Slot()
				.AutoWidth()
				.Padding(8, 0)
				[SNew(SComboBox<TSharedPtr<FString>>)
					.OptionsSource(&EditorPole.BaseMeshNameOptions)
					.OnGenerateWidget_Lambda([](TSharedPtr<FString> InPtr)
					{
						return SNew(STextBlock).Text(FText::FromString(InPtr.IsValid() ? *InPtr : FString()));
					})
					.OnSelectionChanged_Lambda([this, PoleIndex](TSharedPtr<FString> NewSelection, ESelectInfo::Type)
					{
						if (!NewSelection.IsValid() || !Poles.IsValidIndex(PoleIndex))
						{
							return;
						}
						FTLPole& Pole{Poles[PoleIndex]};
						for (UStaticMesh* Mesh : FTLMeshFactory::GetAllBaseMeshesForPole(Pole))
						{
							if (IsValid(Mesh) && Mesh->GetName() == *NewSelection)
							{
								Pole.BasePoleMesh = Mesh;
								if (IsValid(Pole.BasePoleMeshComponent))
								{
									Pole.BasePoleMeshComponent->SetStaticMesh(Mesh);
								}
								break;
							}
						}
						Rebuild();
					})
					[SNew(STextBlock)
						.Text_Lambda([this, PoleIndex]()
						{
							if (!Poles.IsValidIndex(PoleIndex))
							{
								return FText::GetEmpty();
							}
							const FTLPole& Pole{Poles[PoleIndex]};
							return FText::FromString(IsValid(Pole.BasePoleMesh) ? Pole.BasePoleMesh->GetName() : TEXT("Select..."));
						})
					]
				]
			]

			// — Extensible Mesh Combo —
			+ SVerticalBox::Slot()
			.AutoHeight()
			.Padding(2, 4)
			[SNew(SHorizontalBox) +
				SHorizontalBox::Slot()
				.AutoWidth()
				.VAlign(VAlign_Center)
				[SNew(STextBlock)
					.Text(FText::FromString("Extensible Mesh:"))
				] +
				SHorizontalBox::Slot()
				.AutoWidth()
				.Padding(8, 0)
				[SNew(SComboBox<TSharedPtr<FString>>)
					.OptionsSource(&EditorPole.ExtensibleMeshNameOptions)
					.OnGenerateWidget_Lambda([](TSharedPtr<FString> InPtr)
					{
						return SNew(STextBlock).Text(FText::FromString(InPtr.IsValid() ? *InPtr : FString()));
					})
					.OnSelectionChanged_Lambda([this, PoleIndex](TSharedPtr<FString> NewSelection, ESelectInfo::Type)
					{
						if (!NewSelection.IsValid() || !Poles.IsValidIndex(PoleIndex))
						{
							return;
						}
						FTLPole& Pole{Poles[PoleIndex]};
						for (UStaticMesh* Mesh : FTLMeshFactory::GetAllExtensibleMeshesForPole(Pole))
						{
							if (IsValid(Mesh) && Mesh->GetName() == *NewSelection)
							{
								Pole.ExtensiblePoleMesh = Mesh;
								if (IsValid(Pole.ExtensiblePoleMeshComponent))
								{
									Pole.ExtensiblePoleMeshComponent->SetStaticMesh(Mesh);
								}
								break;
							}
						}
						Rebuild();
					})
					[SNew(STextBlock)
						.Text_Lambda([this, PoleIndex]()
						{
							if (!Poles.IsValidIndex(PoleIndex))
							{
								return FText::GetEmpty();
							}
							const FTLPole& Pole{Poles[PoleIndex]};
							return FText::FromString(
								IsValid(Pole.ExtensiblePoleMesh) ? Pole.ExtensiblePoleMesh->GetName() : TEXT("Select..."));
						})
					]
				]
			]
			// — Cap Mesh Combo —
			+ SVerticalBox::Slot()
			.AutoHeight()
			.Padding(2, 4)
			[SNew(SHorizontalBox) +
				SHorizontalBox::Slot()
				.AutoWidth()
				.VAlign(VAlign_Center)
				[SNew(STextBlock)
					.Text(FText::FromString("Cap Mesh:"))
				] +
				SHorizontalBox::Slot()
				.AutoWidth()
				.Padding(8, 0)
				[SNew(SComboBox<TSharedPtr<FString>>)
					.OptionsSource(&EditorPole.CapMeshNameOptions)
					.OnGenerateWidget_Lambda([](TSharedPtr<FString> InPtr)
					{
						return SNew(STextBlock).Text(FText::FromString(InPtr.IsValid() ? *InPtr : FString()));
					})
					.OnSelectionChanged_Lambda([this, PoleIndex](TSharedPtr<FString> NewSelection, ESelectInfo::Type)
					{
						if (!NewSelection.IsValid() || !Poles.IsValidIndex(PoleIndex))
						{
							return;
						}
						FTLPole& Pole{Poles[PoleIndex]};
						for (UStaticMesh* Mesh : FTLMeshFactory::GetAllCapMeshesForPole(Pole))
						{
							if (IsValid(Mesh) && Mesh->GetName() == *NewSelection)
							{
								Pole.CapPoleMesh = Mesh;
								if (IsValid(Pole.CapPoleMeshComponent))
								{
									Pole.CapPoleMeshComponent->SetStaticMesh(Mesh);
								}
								break;
							}
						}
						Rebuild();
					})
					[SNew(STextBlock)
						.Text_Lambda([this, PoleIndex]()
						{
							if (!Poles.IsValidIndex(PoleIndex))
							{
								return FText::GetEmpty();
							}
							const FTLPole& Pole{Poles[PoleIndex]};
							return FText::FromString(IsValid(Pole.CapPoleMesh) ? Pole.CapPoleMesh->GetName() : TEXT("Select..."));
						})
					]
				]
			]
			// — Pole Height
			+ SVerticalBox::Slot()
			.AutoHeight()
			.Padding(2, 4)
			[SNew(SHorizontalBox) +
				SHorizontalBox::Slot()
				.AutoWidth()
				.VAlign(VAlign_Center)
				[SNew(STextBlock)
					.Text(FText::FromString("Pole Height (m):"))
				] +
				SHorizontalBox::Slot()
				.FillWidth(0.3f)
				.Padding(8, 0)
				[SNew(SNumericEntryBox<float>)
					.Value_Lambda([this, PoleIndex]() -> TOptional<float>
					{
						return Poles.IsValidIndex(PoleIndex) ? Poles[PoleIndex].PoleHeight : 0.0f;
					})
					.OnValueCommitted_Lambda([this, PoleIndex](float NewValue, ETextCommit::Type)
					{
						if (!Poles.IsValidIndex(PoleIndex))
						{
							return;
						}
						Poles[PoleIndex].PoleHeight = NewValue;
						Rebuild();
					})
				]
			]
			// — Heads Section —
			+ SVerticalBox::Slot()
			.AutoHeight()
			.Padding(2, 8)
			[SNew(SExpandableArea)
				.InitiallyCollapsed(!EditorPole.bHeadsExpanded)
				.AreaTitle(FText::FromString("Heads"))
				.Padding(4)
				.OnAreaExpansionChanged_Lambda([this, PoleIndex](bool b)
				{
					if (EditorPoles.IsValidIndex(PoleIndex))
					{
						EditorPoles[PoleIndex].bHeadsExpanded = b;
					}
				})
				.BodyContent()[BuildHeadSection(PoleIndex)]
			]
			// — Delete Pole button —
			+ SVerticalBox::Slot()
			.AutoHeight()
			.HAlign(HAlign_Right)
			.Padding(2, 0)
			[SNew(SButton)
				.Text(FText::FromString("Delete Pole"))
				.OnClicked_Lambda([this, PoleIndex]() { return OnDeletePoleClicked(PoleIndex); })
			]
		];
	// clang-format on
}

TSharedRef<SWidget> STrafficLightToolWidget::BuildPoleSection()
{
	SAssignNew(RootContainer, SVerticalBox);

	// clang-format off
	return SNew(SVerticalBox)
    // Load JSON Button
    + SVerticalBox::Slot()
        .AutoHeight()
        .Padding(10)
        [
            SNew(SButton)
            .Text(FText::FromString(TEXT("Import JSON...")))
            .OnClicked(this, &STrafficLightToolWidget::OnImportJSONClicked)
        ]
    // Export JSON Button
	+ SVerticalBox::Slot()
	.AutoHeight()
	.Padding(10)
	[
		SNew(SButton)
		.Text(FText::FromString(TEXT("Export JSON...")))
		.OnClicked(this, &STrafficLightToolWidget::OnExportJSONClicked)
	]
    // — Modify Button — (only visible when actor is selected)
    + SVerticalBox::Slot()
        .AutoHeight()
        .Padding(10)
        [
            SNew(SButton)
            .Visibility(this, &STrafficLightToolWidget::GetModifyVisibility)
            .Text(FText::FromString("Modify"))
            .OnClicked(this, &STrafficLightToolWidget::OnModifyClicked)
        ]
	+ SVerticalBox::Slot()
		.AutoHeight()
		.Padding(10)
		[SNew(SButton)
			.Text(FText::FromString("Add Pole"))
			.OnClicked(this, &STrafficLightToolWidget::OnAddPoleClicked)
		] +
		SVerticalBox::Slot()
		.FillHeight(1.0f)
		.Padding(10)
		[SNew(SScrollBox) + SScrollBox::Slot()[RootContainer.ToSharedRef()]];
	// clang-format on
}

TSharedRef<SWidget> STrafficLightToolWidget::BuildHeadEntry(int32 PoleIndex, int32 HeadIndex)
{
	if (!EditorPoles.IsValidIndex(PoleIndex) || !Poles.IsValidIndex(PoleIndex) ||
		!EditorPoles[PoleIndex].Heads.IsValidIndex(HeadIndex) || !Poles[PoleIndex].Heads.IsValidIndex(HeadIndex))
	{
		return SNullWidget::NullWidget;
	}
	FEditorPole& EditorPole{EditorPoles[PoleIndex]};
	FTLPole& Pole{Poles[PoleIndex]};
	FEditorHead& EditorHead{EditorPole.Heads[HeadIndex]};
	FTLHead& Head{Pole.Heads[HeadIndex]};

	const int32 InitialStyleIdx{static_cast<int32>(Head.Style)};
	const TSharedPtr<FString> InitialStyleItem{
		StyleOptions.IsValidIndex(InitialStyleIdx) ? StyleOptions[InitialStyleIdx] : TSharedPtr<FString>()};
	const int32 InitialAttachIdx{static_cast<int32>(Head.Attachment)};
	const TSharedPtr<FString> InitialAttachItem{
		AttachmentOptions.IsValidIndex(InitialAttachIdx) ? AttachmentOptions[InitialAttachIdx] : TSharedPtr<FString>()};
	const int32 InitialOrientIdx{static_cast<int32>(Head.Orientation)};
	const TSharedPtr<FString> InitialOrientItem{
		OrientationOptions.IsValidIndex(InitialOrientIdx) ? OrientationOptions[InitialOrientIdx] : TSharedPtr<FString>()};

	// clang-format off
	return SNew(SExpandableArea)
		.InitiallyCollapsed(!EditorHead.bExpanded)
		.AreaTitle(FText::FromString(FString::Printf(TEXT("Head #%d"), HeadIndex)))
		.Padding(4)
		.OnAreaExpansionChanged_Lambda([this, PoleIndex, HeadIndex](bool bExpanded)
		{
			if (EditorPoles.IsValidIndex(PoleIndex) && EditorPoles[PoleIndex].Heads.IsValidIndex(HeadIndex))
			{
				EditorPoles[PoleIndex].Heads[HeadIndex].bExpanded = bExpanded;
			}
		})
		.BodyContent()
		[SNew(SBorder)
			.BorderBackgroundColor(FLinearColor{0.15f, 0.15f, 0.15f})
			.Padding(8)
			[SNew(SVerticalBox)
				// — Head Style —
				+ SVerticalBox::Slot()
				.AutoHeight()
				.Padding(2)
				[SNew(STextBlock)
					.Text(FText::FromString("Head Style"))
				] +
				SVerticalBox::Slot()
				.AutoHeight()
				.Padding(2)
				[SNew(SComboBox<TSharedPtr<FString>>)
					.OptionsSource(&StyleOptions)
					.InitiallySelectedItem(InitialStyleItem)
					.OnGenerateWidget_Lambda([](TSharedPtr<FString> In)
					{
						return SNew(STextBlock).Text(FText::FromString(In.IsValid() ? *In : FString()));
					})
					.OnSelectionChanged_Lambda([this, PoleIndex, HeadIndex](TSharedPtr<FString> NewSelection, ESelectInfo::Type)
					{
						if (!NewSelection.IsValid() || !Poles.IsValidIndex(PoleIndex) || !Poles[PoleIndex].Heads.IsValidIndex(HeadIndex))
						{
							return;
						}
						const int32 Choice{StyleOptions.IndexOfByPredicate([&](const auto& S) { return S == NewSelection; })};
						if (Choice == INDEX_NONE)
						{
							return;
						}
						OnHeadStyleChanged(static_cast<ETLStyle>(Choice), PoleIndex, HeadIndex);
					})
					[SNew(STextBlock)
						.Text_Lambda([this, PoleIndex, HeadIndex]()
						{
							if (!Poles.IsValidIndex(PoleIndex) || !Poles[PoleIndex].Heads.IsValidIndex(HeadIndex))
							{
								return FText::GetEmpty();
							}
							const int32 Idx{static_cast<int32>(Poles[PoleIndex].Heads[HeadIndex].Style)};
							return FText::FromString(
								(StyleOptions.IsValidIndex(Idx) && StyleOptions[Idx].IsValid()) ? *StyleOptions[Idx] : FString());
						})
					]
				]
				// — Attachment Type —
				+ SVerticalBox::Slot()
				.AutoHeight()
				.Padding(2)
				[SNew(STextBlock)
					.Text(FText::FromString("Attachment"))
				] +
				SVerticalBox::Slot()
				.AutoHeight()
				.Padding(2)
				[SNew(SComboBox<TSharedPtr<FString>>)
					.OptionsSource(&AttachmentOptions)
					.InitiallySelectedItem(InitialAttachItem)
					.OnGenerateWidget_Lambda([](TSharedPtr<FString> In)
					{
						return SNew(STextBlock).Text(FText::FromString(In.IsValid() ? *In : FString()));
					})
					.OnSelectionChanged_Lambda([this, PoleIndex, HeadIndex](TSharedPtr<FString> NewSelection, ESelectInfo::Type)
					{
						if (!NewSelection.IsValid() || !Poles.IsValidIndex(PoleIndex) || !Poles[PoleIndex].Heads.IsValidIndex(HeadIndex))
						{
							return;
						}
						const int32 Choice{AttachmentOptions.IndexOfByPredicate([&](const auto& S) { return S == NewSelection; })};
						if (Choice == INDEX_NONE)
						{
							return;
						}
						Poles[PoleIndex].Heads[HeadIndex].Attachment = static_cast<ETLHeadAttachment>(Choice);
						Rebuild();
					})
					[SNew(STextBlock)
						.Text_Lambda([this, PoleIndex, HeadIndex]()
						{
							if (!Poles.IsValidIndex(PoleIndex) || !Poles[PoleIndex].Heads.IsValidIndex(HeadIndex))
							{
								return FText::GetEmpty();
							}
							const int32 Idx{static_cast<int32>(Poles[PoleIndex].Heads[HeadIndex].Attachment)};
							return FText::FromString((AttachmentOptions.IsValidIndex(Idx) && AttachmentOptions[Idx].IsValid())
									? *AttachmentOptions[Idx]
									: FString());
						})
					]
				]
				// — Orientation —
				+ SVerticalBox::Slot()
				.AutoHeight()
				.Padding(2)
				[SNew(STextBlock)
					.Text(FText::FromString("Orientation"))
				] +
				SVerticalBox::Slot()
				.AutoHeight()
				.Padding(2)
				[SNew(SComboBox<TSharedPtr<FString>>)
					.OptionsSource(&OrientationOptions)
					.InitiallySelectedItem(InitialOrientItem)
					.OnGenerateWidget_Lambda([](TSharedPtr<FString> In)
					{
						return SNew(STextBlock).Text(FText::FromString(In.IsValid() ? *In : FString()));
					})
					.OnSelectionChanged_Lambda([this, PoleIndex, HeadIndex](TSharedPtr<FString> NewSelection, ESelectInfo::Type)
					{
						if (!NewSelection.IsValid() || !Poles.IsValidIndex(PoleIndex) || !Poles[PoleIndex].Heads.IsValidIndex(HeadIndex))
						{
							return;
						}
						const int32 Choice{OrientationOptions.IndexOfByPredicate([&](const auto& S) { return S == NewSelection; })};
						if (Choice == INDEX_NONE)
						{
							return;
						}
						OnHeadOrientationChanged(static_cast<ETLOrientation>(Choice), PoleIndex, HeadIndex);
					})
					[SNew(STextBlock)
						.Text_Lambda([this, PoleIndex, HeadIndex]()
						{
							if (!Poles.IsValidIndex(PoleIndex) || !Poles[PoleIndex].Heads.IsValidIndex(HeadIndex))
							{
								return FText::GetEmpty();
							}
							const int32 Idx{static_cast<int32>(Poles[PoleIndex].Heads[HeadIndex].Orientation)};
							return FText::FromString((OrientationOptions.IsValidIndex(Idx) && OrientationOptions[Idx].IsValid())
									? *OrientationOptions[Idx]
									: FString());
						})
					]
				]
				// — Backplate —
				+ SVerticalBox::Slot()
				.AutoHeight()
				.Padding(2)
				[SNew(SCheckBox)
					.IsChecked_Lambda([this, PoleIndex, HeadIndex]()
					{
						if (!Poles.IsValidIndex(PoleIndex) || !Poles[PoleIndex].Heads.IsValidIndex(HeadIndex))
						{
							return ECheckBoxState::Unchecked;
						}
						return Poles[PoleIndex].Heads[HeadIndex].bHasBackplate ? ECheckBoxState::Checked : ECheckBoxState::Unchecked;
					})
					.OnCheckStateChanged_Lambda([this, PoleIndex, HeadIndex](ECheckBoxState NewState)
					{
						if (!Poles.IsValidIndex(PoleIndex) || !Poles[PoleIndex].Heads.IsValidIndex(HeadIndex))
						{
							return;
						}
						Poles[PoleIndex].Heads[HeadIndex].bHasBackplate = (NewState == ECheckBoxState::Checked);
						Rebuild();
					})
					[SNew(STextBlock)
						.Text(FText::FromString("Has Backplate"))
					]
				]
				// Relative Location
				+ SVerticalBox::Slot()
				.AutoHeight()
				.Padding(2)
				[SNew(STextBlock)
					.Text(FText::FromString("Relative Location"))
				] +
				SVerticalBox::Slot()
				.AutoHeight()
				.Padding(2)
				[SNew(SHorizontalBox)
					+ SHorizontalBox::Slot()
					.FillWidth(1)
					[SNew(SNumericEntryBox<float>)
						.Value_Lambda([this, PoleIndex, HeadIndex]() -> TOptional<float>
						{
							return (Poles.IsValidIndex(PoleIndex) && Poles[PoleIndex].Heads.IsValidIndex(HeadIndex))
								? static_cast<float>(Poles[PoleIndex].Heads[HeadIndex].Transform.GetLocation().X)
								: 0.0f;
						})
						.OnValueCommitted_Lambda([this, PoleIndex, HeadIndex](float V, ETextCommit::Type)
						{
							if (!Poles.IsValidIndex(PoleIndex) || !Poles[PoleIndex].Heads.IsValidIndex(HeadIndex))
							{
								return;
							}
							FTLHead& Head{Poles[PoleIndex].Heads[HeadIndex]};
							FVector L{Head.Transform.GetLocation()};
							L.X = V;
							Head.Transform.SetLocation(L);
							Rebuild();
						})
					] +
					SHorizontalBox::Slot()
					.FillWidth(1)
					[SNew(SNumericEntryBox<float>)
						.Value_Lambda([this, PoleIndex, HeadIndex]() -> TOptional<float>
						{
							return (Poles.IsValidIndex(PoleIndex) && Poles[PoleIndex].Heads.IsValidIndex(HeadIndex))
								? static_cast<float>(Poles[PoleIndex].Heads[HeadIndex].Transform.GetLocation().Y)
								: 0.0f;
						})
						.OnValueCommitted_Lambda([this, PoleIndex, HeadIndex](float V, ETextCommit::Type)
						{
							if (!Poles.IsValidIndex(PoleIndex) || !Poles[PoleIndex].Heads.IsValidIndex(HeadIndex))
							{
								return;
							}
							FTLHead& Head{Poles[PoleIndex].Heads[HeadIndex]};
							FVector L{Head.Transform.GetLocation()};
							L.Y = V;
							Head.Transform.SetLocation(L);
							Rebuild();
						})
					] +
					SHorizontalBox::Slot()
					.FillWidth(1)
					[SNew(SNumericEntryBox<float>)
						.Value_Lambda([this, PoleIndex, HeadIndex]() -> TOptional<float>
						{
							return (Poles.IsValidIndex(PoleIndex) && Poles[PoleIndex].Heads.IsValidIndex(HeadIndex))
								? static_cast<float>(Poles[PoleIndex].Heads[HeadIndex].Transform.GetLocation().Z)
								: 0.0f;
						})
						.OnValueCommitted_Lambda([this, PoleIndex, HeadIndex](float V, ETextCommit::Type)
						{
							if (!Poles.IsValidIndex(PoleIndex) || !Poles[PoleIndex].Heads.IsValidIndex(HeadIndex))
							{
								return;
							}
							FTLHead& Head{Poles[PoleIndex].Heads[HeadIndex]};
							FVector L{Head.Transform.GetLocation()};
							L.Z = V;
							Head.Transform.SetLocation(L);
							Rebuild();
						})
					]
				]
                // Relative Rotation
				+ SVerticalBox::Slot()
				.AutoHeight()
				.Padding(2)
				[SNew(STextBlock)
					.Text(FText::FromString("Relative Rotation"))
				] +
				SVerticalBox::Slot()
				.AutoHeight()
				.Padding(2)
				[SNew(SHorizontalBox)
					+ SHorizontalBox::Slot()
					.FillWidth(1)
					[SNew(SNumericEntryBox<double>)
						.Value_Lambda([this, PoleIndex, HeadIndex]() -> TOptional<double>
						{
							return (Poles.IsValidIndex(PoleIndex) && Poles[PoleIndex].Heads.IsValidIndex(HeadIndex))
								? Poles[PoleIndex].Heads[HeadIndex].Transform.GetRotation().Rotator().Pitch
								: 0.0;
						})
						.OnValueCommitted_Lambda([this, PoleIndex, HeadIndex](double V, ETextCommit::Type)
						{
							if (!Poles.IsValidIndex(PoleIndex) || !Poles[PoleIndex].Heads.IsValidIndex(HeadIndex))
							{
								return;
							}
							FTLHead& Head{Poles[PoleIndex].Heads[HeadIndex]};
							FRotator3d R{Head.Transform.GetRotation().Rotator()};
							R.Pitch = V;
							Head.Transform.SetRotation(R.Quaternion());
							Rebuild();
						})
					] +
					SHorizontalBox::Slot()
					.FillWidth(1)
					[SNew(SNumericEntryBox<double>)
						.Value_Lambda([this, PoleIndex, HeadIndex]() -> TOptional<double>
						{
							return (Poles.IsValidIndex(PoleIndex) && Poles[PoleIndex].Heads.IsValidIndex(HeadIndex))
								? Poles[PoleIndex].Heads[HeadIndex].Transform.GetRotation().Rotator().Yaw
								: 0.0;
						})
						.OnValueCommitted_Lambda([this, PoleIndex, HeadIndex](double V, ETextCommit::Type)
						{
							if (!Poles.IsValidIndex(PoleIndex) || !Poles[PoleIndex].Heads.IsValidIndex(HeadIndex))
							{
								return;
							}
							FTLHead& Head{Poles[PoleIndex].Heads[HeadIndex]};
							FRotator3d R{Head.Transform.GetRotation().Rotator()};
							R.Yaw = V;
							Head.Transform.SetRotation(R.Quaternion());
							Rebuild();
						})
					] +
					SHorizontalBox::Slot()
					.FillWidth(1)
					[SNew(SNumericEntryBox<double>)
						.Value_Lambda([this, PoleIndex, HeadIndex]() -> TOptional<double>
						{
							return (Poles.IsValidIndex(PoleIndex) && Poles[PoleIndex].Heads.IsValidIndex(HeadIndex))
								? Poles[PoleIndex].Heads[HeadIndex].Transform.GetRotation().Rotator().Roll
								: 0.0;
						})
						.OnValueCommitted_Lambda([this, PoleIndex, HeadIndex](double V, ETextCommit::Type)
						{
							if (!Poles.IsValidIndex(PoleIndex) || !Poles[PoleIndex].Heads.IsValidIndex(HeadIndex))
							{
								return;
							}
							FTLHead& Head{Poles[PoleIndex].Heads[HeadIndex]};
							FRotator3d R{Head.Transform.GetRotation().Rotator()};
							R.Roll = V;
							Head.Transform.SetRotation(R.Quaternion());
							Rebuild();
						})
					]
				]
                // Relative Scale
				+ SVerticalBox::Slot()
				.AutoHeight()
				.Padding(2)
				[SNew(STextBlock)
					.Text(FText::FromString("Relative Scale"))
				] +
				SVerticalBox::Slot()
				.AutoHeight()
				.Padding(2)
				[SNew(SHorizontalBox)
					+ SHorizontalBox::Slot()
					.FillWidth(1)
					[SNew(SNumericEntryBox<double>)
						.Value_Lambda([this, PoleIndex, HeadIndex]() -> TOptional<double>
						{
							return (Poles.IsValidIndex(PoleIndex) && Poles[PoleIndex].Heads.IsValidIndex(HeadIndex))
								? Poles[PoleIndex].Heads[HeadIndex].Transform.GetScale3D().X
								: 1.0;
						})
						.OnValueCommitted_Lambda([this, PoleIndex, HeadIndex](double V, ETextCommit::Type)
						{
							if (!Poles.IsValidIndex(PoleIndex) || !Poles[PoleIndex].Heads.IsValidIndex(HeadIndex))
							{
								return;
							}
							FTLHead& Head{Poles[PoleIndex].Heads[HeadIndex]};
							FVector L{Head.Transform.GetScale3D()};
							L.X = V;
							Head.Transform.SetScale3D(L);
							Rebuild();
						})
					] +
					SHorizontalBox::Slot()
					.FillWidth(1)
					[SNew(SNumericEntryBox<double>)
						.Value_Lambda([this, PoleIndex, HeadIndex]() -> TOptional<double>
						{
							return (Poles.IsValidIndex(PoleIndex) && Poles[PoleIndex].Heads.IsValidIndex(HeadIndex))
								? Poles[PoleIndex].Heads[HeadIndex].Transform.GetScale3D().Y
								: 1.0;
						})
						.OnValueCommitted_Lambda([this, PoleIndex, HeadIndex](double V, ETextCommit::Type)
						{
							if (!Poles.IsValidIndex(PoleIndex) || !Poles[PoleIndex].Heads.IsValidIndex(HeadIndex))
							{
								return;
							}
							FTLHead& Head{Poles[PoleIndex].Heads[HeadIndex]};
							FVector L{Head.Transform.GetScale3D()};
							L.Y = V;
							Head.Transform.SetScale3D(L);
							Rebuild();
						})
					] +
					SHorizontalBox::Slot()
					.FillWidth(1)
					[SNew(SNumericEntryBox<double>)
						.Value_Lambda([this, PoleIndex, HeadIndex]() -> TOptional<double>
						{
							return (Poles.IsValidIndex(PoleIndex) && Poles[PoleIndex].Heads.IsValidIndex(HeadIndex))
								? Poles[PoleIndex].Heads[HeadIndex].Transform.GetScale3D().Z
								: 1.0;
						})
						.OnValueCommitted_Lambda([this, PoleIndex, HeadIndex](double V, ETextCommit::Type)
						{
							if (!Poles.IsValidIndex(PoleIndex) || !Poles[PoleIndex].Heads.IsValidIndex(HeadIndex))
							{
								return;
							}
							FTLHead& Head{Poles[PoleIndex].Heads[HeadIndex]};
							FVector L{Head.Transform.GetScale3D()};
							L.Z = V;
							Head.Transform.SetScale3D(L);
							Rebuild();
						})
					]
				]
				// Offset Position
				+ SVerticalBox::Slot()
				.AutoHeight()
				.Padding(2)
				[SNew(STextBlock)
					.Text(FText::FromString("Offset Position"))
				] +
				SVerticalBox::Slot()
				.AutoHeight()
				.Padding(2)
				[SNew(SHorizontalBox) +
					SHorizontalBox::Slot()
					.FillWidth(1)
					[SNew(SNumericEntryBox<float>)
						.Value_Lambda([this, PoleIndex, HeadIndex]() -> TOptional<float>
						{
							return (Poles.IsValidIndex(PoleIndex) && Poles[PoleIndex].Heads.IsValidIndex(HeadIndex))
								? static_cast<float>(Poles[PoleIndex].Heads[HeadIndex].Offset.GetLocation().X)
								: 0.0f;
						})
						.OnValueCommitted_Lambda([this, PoleIndex, HeadIndex](float V, ETextCommit::Type)
						{
							if (!Poles.IsValidIndex(PoleIndex) || !Poles[PoleIndex].Heads.IsValidIndex(HeadIndex))
							{
								return;
							}
							FTLHead& Head{Poles[PoleIndex].Heads[HeadIndex]};
							FVector L{Head.Offset.GetLocation()};
							L.X = V;
							Head.Offset.SetLocation(L);
							Rebuild();
						})
					] +
					SHorizontalBox::Slot()
					.FillWidth(1)
					[SNew(SNumericEntryBox<float>)
						.Value_Lambda([this, PoleIndex, HeadIndex]() -> TOptional<float>
						{
							return (Poles.IsValidIndex(PoleIndex) && Poles[PoleIndex].Heads.IsValidIndex(HeadIndex))
								? static_cast<float>(Poles[PoleIndex].Heads[HeadIndex].Offset.GetLocation().Y)
								: 0.0f;
						})
						.OnValueCommitted_Lambda([this, PoleIndex, HeadIndex](float V, ETextCommit::Type)
						{
							if (!Poles.IsValidIndex(PoleIndex) || !Poles[PoleIndex].Heads.IsValidIndex(HeadIndex))
							{
								return;
							}
							FTLHead& Head{Poles[PoleIndex].Heads[HeadIndex]};
							FVector L{Head.Offset.GetLocation()};
							L.Y = V;
							Head.Offset.SetLocation(L);
							Rebuild();
						})
					] +
					SHorizontalBox::Slot()
					.FillWidth(1)
					[SNew(SNumericEntryBox<float>)
						.Value_Lambda([this, PoleIndex, HeadIndex]() -> TOptional<float>
						{
							return (Poles.IsValidIndex(PoleIndex) && Poles[PoleIndex].Heads.IsValidIndex(HeadIndex))
								? static_cast<float>(Poles[PoleIndex].Heads[HeadIndex].Offset.GetLocation().Z)
								: 0.0f;
						})
						.OnValueCommitted_Lambda([this, PoleIndex, HeadIndex](float V, ETextCommit::Type)
						{
							if (!Poles.IsValidIndex(PoleIndex) || !Poles[PoleIndex].Heads.IsValidIndex(HeadIndex))
							{
								return;
							}
							FTLHead& Head{Poles[PoleIndex].Heads[HeadIndex]};
							FVector L{Head.Offset.GetLocation()};
							L.Z = V;
							Head.Offset.SetLocation(L);
							Rebuild();
						})
					]
				] +
				SVerticalBox::Slot()
				.AutoHeight()
				.Padding(2)
				[SNew(STextBlock)
					.Text(FText::FromString("Offset Rotation"))
				] +
				SVerticalBox::Slot()
				.AutoHeight()
				.Padding(2)
				[SNew(SHorizontalBox) +
					SHorizontalBox::Slot()
					.FillWidth(1)
					[SNew(SNumericEntryBox<float>)
						.Value_Lambda([this, PoleIndex, HeadIndex]() -> TOptional<float>
						{
							return (Poles.IsValidIndex(PoleIndex) && Poles[PoleIndex].Heads.IsValidIndex(HeadIndex))
								? static_cast<float>(Poles[PoleIndex].Heads[HeadIndex].Offset.Rotator().Pitch)
								: 0.0f;
						})
						.OnValueCommitted_Lambda([this, PoleIndex, HeadIndex](float V, ETextCommit::Type)
						{
							if (!Poles.IsValidIndex(PoleIndex) || !Poles[PoleIndex].Heads.IsValidIndex(HeadIndex))
							{
								return;
							}
							FTLHead& Head{Poles[PoleIndex].Heads[HeadIndex]};
							FRotator R{Head.Offset.Rotator()};
							R.Pitch = V;
							Head.Offset.SetRotation(FQuat{R});
							Rebuild();
						})
					] +
					SHorizontalBox::Slot()
					.FillWidth(1)
					[SNew(SNumericEntryBox<float>)
						.Value_Lambda([this, PoleIndex, HeadIndex]() -> TOptional<float>
						{
							return (Poles.IsValidIndex(PoleIndex) && Poles[PoleIndex].Heads.IsValidIndex(HeadIndex))
								? static_cast<float>(Poles[PoleIndex].Heads[HeadIndex].Offset.Rotator().Yaw)
								: 0.0f;
						})
						.OnValueCommitted_Lambda([this, PoleIndex, HeadIndex](float V, ETextCommit::Type)
						{
							if (!Poles.IsValidIndex(PoleIndex) || !Poles[PoleIndex].Heads.IsValidIndex(HeadIndex))
							{
								return;
							}
							FTLHead& Head{Poles[PoleIndex].Heads[HeadIndex]};
							FRotator R{Head.Offset.Rotator()};
							R.Yaw = V;
							Head.Offset.SetRotation(FQuat{R});
							Rebuild();
						})
					] +
					SHorizontalBox::Slot()
					.FillWidth(1)
					[SNew(SNumericEntryBox<float>)
						.Value_Lambda([this, PoleIndex, HeadIndex]() -> TOptional<float>
						{
							return (Poles.IsValidIndex(PoleIndex) && Poles[PoleIndex].Heads.IsValidIndex(HeadIndex))
								? static_cast<float>(Poles[PoleIndex].Heads[HeadIndex].Offset.Rotator().Roll)
								: 0.0f;
						})
						.OnValueCommitted_Lambda([this, PoleIndex, HeadIndex](float V, ETextCommit::Type)
						{
							if (!Poles.IsValidIndex(PoleIndex) || !Poles[PoleIndex].Heads.IsValidIndex(HeadIndex))
							{
								return;
							}
							FTLHead& Head{Poles[PoleIndex].Heads[HeadIndex]};
							FRotator R{Head.Offset.Rotator()};
							R.Roll = V;
							Head.Offset.SetRotation(FQuat{R});
							Rebuild();
						})
					]
				] +
				// Offset Scale
				SVerticalBox::Slot()
				.AutoHeight()
				.Padding(2)
				[SNew(STextBlock)
					.Text(FText::FromString("Offset Scale"))
				] +
				SVerticalBox::Slot()
				.AutoHeight()
				.Padding(2)
				[SNew(SHorizontalBox) +
					SHorizontalBox::Slot()
					.FillWidth(1)
					[SNew(SNumericEntryBox<float>)
						.Value_Lambda([this, PoleIndex, HeadIndex]() -> TOptional<float>
						{
							return (Poles.IsValidIndex(PoleIndex) && Poles[PoleIndex].Heads.IsValidIndex(HeadIndex))
								? static_cast<float>(Poles[PoleIndex].Heads[HeadIndex].Offset.GetScale3D().X)
								: 1.0f;
						})
						.OnValueCommitted_Lambda([this, PoleIndex, HeadIndex](float V, ETextCommit::Type)
						{
							if (!Poles.IsValidIndex(PoleIndex) || !Poles[PoleIndex].Heads.IsValidIndex(HeadIndex))
							{
								return;
							}
							FTLHead& Head{Poles[PoleIndex].Heads[HeadIndex]};
							FVector S{Head.Offset.GetScale3D()};
							S.X = V;
							Head.Offset.SetScale3D(S);
							Rebuild();
						})
					] +
					SHorizontalBox::Slot()
					.FillWidth(1)
					[SNew(SNumericEntryBox<float>)
						.Value_Lambda([this, PoleIndex, HeadIndex]() -> TOptional<float>
						{
							return (Poles.IsValidIndex(PoleIndex) && Poles[PoleIndex].Heads.IsValidIndex(HeadIndex))
								? static_cast<float>(Poles[PoleIndex].Heads[HeadIndex].Offset.GetScale3D().Y)
								: 1.0f;
						})
						.OnValueCommitted_Lambda([this, PoleIndex, HeadIndex](float V, ETextCommit::Type)
						{
							if (!Poles.IsValidIndex(PoleIndex) || !Poles[PoleIndex].Heads.IsValidIndex(HeadIndex))
							{
								return;
							}
							FTLHead& Head{Poles[PoleIndex].Heads[HeadIndex]};
							FVector S{Head.Offset.GetScale3D()};
							S.Y = V;
							Head.Offset.SetScale3D(S);
							Rebuild();
						})
					] +
					SHorizontalBox::Slot()
					.FillWidth(1)
					[SNew(SNumericEntryBox<float>)
						.Value_Lambda([this, PoleIndex, HeadIndex]() -> TOptional<float>
						{
							return (Poles.IsValidIndex(PoleIndex) && Poles[PoleIndex].Heads.IsValidIndex(HeadIndex))
								? static_cast<float>(Poles[PoleIndex].Heads[HeadIndex].Offset.GetScale3D().Z)
								: 1.0f;
						})
						.OnValueCommitted_Lambda([this, PoleIndex, HeadIndex](float V, ETextCommit::Type)
						{
							if (!Poles.IsValidIndex(PoleIndex) || !Poles[PoleIndex].Heads.IsValidIndex(HeadIndex))
							{
								return;
							}
							FTLHead& Head{Poles[PoleIndex].Heads[HeadIndex]};
							FVector S{Head.Offset.GetScale3D()};
							S.Z = V;
							Head.Offset.SetScale3D(S);
							Rebuild();
						})
					]
				] +
				// — Add + Modules + Delete —
				SVerticalBox::Slot()
				.AutoHeight()
				.HAlign(HAlign_Right)
				.Padding(2, 4, 2, 0)
				[SNew(SVerticalBox)
					// Add Module
					+ SVerticalBox::Slot()
					.AutoHeight()
					.HAlign(HAlign_Right)
					.Padding(2, 4)
					[SNew(SButton)
						.Text(FText::FromString("Add Module"))
						.OnClicked(this, &STrafficLightToolWidget::OnAddModuleClicked, PoleIndex, HeadIndex)
					]
					// Expandable “Modules” section
					+ SVerticalBox::Slot()
					.AutoHeight()
					.Padding(2, 8)
					[SNew(SExpandableArea)
						.InitiallyCollapsed(!EditorHead.bModulesExpanded)
						.AreaTitle(FText::FromString("Modules"))
						.Padding(4)
						.OnAreaExpansionChanged_Lambda([this, PoleIndex, HeadIndex](bool bExp)
						{
							if (EditorPoles.IsValidIndex(PoleIndex) && EditorPoles[PoleIndex].Heads.IsValidIndex(HeadIndex))
							{
								EditorPoles[PoleIndex].Heads[HeadIndex].bModulesExpanded = bExp;
							}
						})
						.BodyContent()[BuildModuleSection(PoleIndex, HeadIndex)]
					]
					// Delete Head
					+ SVerticalBox::Slot()
					.AutoHeight()
					.Padding(2, 0)
					[SNew(SButton)
						.Text(FText::FromString("Delete Head"))
						.OnClicked(this, &STrafficLightToolWidget::OnDeleteHeadClicked, PoleIndex, HeadIndex)
					]
				]
			]
		];
	//clang-format on
}

TSharedRef<SWidget> STrafficLightToolWidget::BuildHeadSection(int32 PoleIndex)
{
	if (!EditorPoles.IsValidIndex(PoleIndex) || !Poles.IsValidIndex(PoleIndex))
	{
		return SNullWidget::NullWidget;
	}
	FEditorPole& EditorPole{EditorPoles[PoleIndex]};

	TSharedRef<SVerticalBox> Container{SNew(SVerticalBox)};

	//clang-format off
	// — Add Head button —
	Container->AddSlot()
	.AutoHeight()
	.Padding(2)
	[SNew(SHorizontalBox)+
		SHorizontalBox::Slot()
		.AutoWidth()
		.VAlign(VAlign_Center)
		[SNew(SButton)
			.Text(FText::FromString("Add Head"))
			.OnClicked(this, &STrafficLightToolWidget::OnAddHeadClicked, PoleIndex)
		]
	];

	// — Scrollable list of head entries —
	Container->AddSlot()
	.FillHeight(1.0f)
	.Padding(2)
	[SNew(SScrollBox) +
		SScrollBox::Slot()
		[SAssignNew(EditorPole.Container, SVerticalBox)
		]
	];

	EditorPole.Container->ClearChildren();
	const int32 NumHeads{FMath::Min(EditorPole.Heads.Num(), Poles[PoleIndex].Heads.Num())};
	for (int32 HeadIndex{0}; HeadIndex < NumHeads; ++HeadIndex)
	{
		EditorPole.Container->AddSlot().AutoHeight().Padding(4, 2)[BuildHeadEntry(PoleIndex, HeadIndex)];
	}

	return Container;
	// clang-format on
}

TSharedRef<SWidget> STrafficLightToolWidget::BuildModuleEntry(int32 PoleIndex, int32 HeadIndex, int32 ModuleIndex)
{
	if (!EditorPoles.IsValidIndex(PoleIndex) || !Poles.IsValidIndex(PoleIndex) ||
		!EditorPoles[PoleIndex].Heads.IsValidIndex(HeadIndex) || !Poles[PoleIndex].Heads.IsValidIndex(HeadIndex) ||
		!EditorPoles[PoleIndex].Heads[HeadIndex].Modules.IsValidIndex(ModuleIndex) ||
		!Poles[PoleIndex].Heads[HeadIndex].Modules.IsValidIndex(ModuleIndex))
	{
		return SNullWidget::NullWidget;
	}
	FEditorPole& EditorPole{EditorPoles[PoleIndex]};
	FTLPole& Pole{Poles[PoleIndex]};
	FEditorHead& EditorHead{EditorPole.Heads[HeadIndex]};
	FTLHead& Head{Pole.Heads[HeadIndex]};
	FEditorModule& EditorModule{EditorHead.Modules[ModuleIndex]};
	FTLModule& Module{Head.Modules[ModuleIndex]};

	TArray<TSharedPtr<FString>>& ModuleMeshNames{EditorModule.MeshNameOptions};
	const TArray<UStaticMesh*> ModuleMeshes{FTLMeshFactory::GetAllMeshesForModule(Head, Module)};
	ModuleMeshNames.Empty();
	for (const UStaticMesh* Mesh : ModuleMeshes)
	{
		if (IsValid(Mesh))
		{
			ModuleMeshNames.Add(MakeShared<FString>(Mesh->GetName()));
		}
	}

	// clang-format off
	return SNew(SBorder)
	.Padding(4)
	[SNew(SHorizontalBox) +
		SHorizontalBox::Slot()
		.AutoWidth()
		.VAlign(VAlign_Center)
		[SNew(SVerticalBox) +
			SVerticalBox::Slot()
			.AutoHeight()
			[SNew(SButton)
				.ButtonStyle(FAppStyle::Get(), "SimpleButton")
				.ContentPadding(2)
				.IsEnabled(ModuleIndex > 0)
				.OnClicked(this, &STrafficLightToolWidget::OnMoveModuleUpClicked, PoleIndex, HeadIndex, ModuleIndex)
				[SNew(SImage)
					.Image(FAppStyle::Get().GetBrush("Icons.ArrowUp"))
				]
			] +
			SVerticalBox::Slot()
			.AutoHeight()
			[SNew(SButton)
				.ButtonStyle(FAppStyle::Get(), "SimpleButton")
				.ContentPadding(2)
				.IsEnabled(ModuleIndex < Head.Modules.Num() - 1)
				.OnClicked(this, &STrafficLightToolWidget::OnMoveModuleDownClicked, PoleIndex, HeadIndex, ModuleIndex)
				[SNew(SImage)
					.Image(FAppStyle::Get()
					.GetBrush("Icons.ArrowDown"))
				]
			]
		]
		+SHorizontalBox::Slot()
		.FillWidth(1)
		.VAlign(VAlign_Center)
		[SNew(SVerticalBox)
			// — Visor checkbox —
			+ SVerticalBox::Slot()
			.AutoHeight()
			.Padding(0, 2)
			[SNew(SCheckBox)
				.IsChecked_Lambda([this, PoleIndex, HeadIndex, ModuleIndex]()
				{
					if (!Poles.IsValidIndex(PoleIndex) || !Poles[PoleIndex].Heads.IsValidIndex(HeadIndex) ||
						!Poles[PoleIndex].Heads[HeadIndex].Modules.IsValidIndex(ModuleIndex))
					{
						return ECheckBoxState::Unchecked;
					}
					return Poles[PoleIndex].Heads[HeadIndex].Modules[ModuleIndex].bHasVisor
						? ECheckBoxState::Checked
						: ECheckBoxState::Unchecked;
				})
				.OnCheckStateChanged(this, &STrafficLightToolWidget::OnModuleVisorChanged, PoleIndex, HeadIndex, ModuleIndex)
				[SNew(STextBlock).
					Text(FText::FromString("Has Visor"))
				]
			]
			// — Mesh Combo
			+ SVerticalBox::Slot()
			.AutoHeight()
			.Padding(0,2)
			[
				SNew(SHorizontalBox)
					+ SHorizontalBox::Slot()
					.AutoWidth()
					.VAlign(VAlign_Center)
					[SNew(STextBlock)
							.Text(FText::FromString("Mesh:"))
					]
					+ SHorizontalBox::Slot()
						.FillWidth(1)
						.Padding(8,0)
					[SNew(SComboBox<TSharedPtr<FString>>)
						.OptionsSource(&ModuleMeshNames)
						.OnGenerateWidget_Lambda([](TSharedPtr<FString> InPtr)
						{
							return SNew(STextBlock).Text(FText::FromString(InPtr.IsValid() ? *InPtr : FString()));
						})
						.OnSelectionChanged_Lambda([this, PoleIndex, HeadIndex, ModuleIndex](TSharedPtr<FString> NewSelection, ESelectInfo::Type)
						{
							if (!NewSelection.IsValid() || !EditorPoles.IsValidIndex(PoleIndex) || !Poles.IsValidIndex(PoleIndex) ||
								!EditorPoles[PoleIndex].Heads.IsValidIndex(HeadIndex) || !Poles[PoleIndex].Heads.IsValidIndex(HeadIndex) ||
								!EditorPoles[PoleIndex].Heads[HeadIndex].Modules.IsValidIndex(ModuleIndex) ||
								!Poles[PoleIndex].Heads[HeadIndex].Modules.IsValidIndex(ModuleIndex))
							{
								return;
							}

							FTLPole& Pole     {Poles[PoleIndex]};
							FTLHead& Head     {Pole.Heads[HeadIndex]};
							FTLModule& Module {Head.Modules[ModuleIndex]};
							FEditorModule& EditorModule{EditorPoles[PoleIndex].Heads[HeadIndex].Modules[ModuleIndex]};

							for (UStaticMesh* Mesh : FTLMeshFactory::GetAllMeshesForModule(Head, Module))
							{
								if (IsValid(Mesh) && Mesh->GetName() == *NewSelection)
								{
									Module.ModuleMesh = Mesh;
									break;
								}
							}

							SyncModuleLightsWithMesh(Module, EditorModule);
							Rebuild();
						})
						[
							SNew(STextBlock)
								.Text_Lambda([this, PoleIndex, HeadIndex, ModuleIndex]()
								{
									if (!Poles.IsValidIndex(PoleIndex) || !Poles[PoleIndex].Heads.IsValidIndex(HeadIndex) ||
										!Poles[PoleIndex].Heads[HeadIndex].Modules.IsValidIndex(ModuleIndex))
									{
										return FText::GetEmpty();
									}
									const FTLModule& Module {Poles[PoleIndex].Heads[HeadIndex].Modules[ModuleIndex]};
									return FText::FromString(IsValid(Module.ModuleMesh) ? Module.ModuleMesh->GetName() : TEXT("Select..."));
								})
						]
					]
			]
			+ SVerticalBox::Slot()
			.AutoHeight()
			.Padding(0,2)
			[
				SNew(SExpandableArea)
				.InitiallyCollapsed(!EditorModule.bLightsExpanded)
				.AreaTitle(FText::FromString("Lights"))
				.Padding(4)
				.OnAreaExpansionChanged_Lambda([this, PoleIndex, HeadIndex, ModuleIndex](bool b)
				{
					if (EditorPoles.IsValidIndex(PoleIndex) && EditorPoles[PoleIndex].Heads.IsValidIndex(HeadIndex) &&
						EditorPoles[PoleIndex].Heads[HeadIndex].Modules.IsValidIndex(ModuleIndex))
					{
						EditorPoles[PoleIndex].Heads[HeadIndex].Modules[ModuleIndex].bLightsExpanded = b;
					}
				})
				.BodyContent()
				[
					BuildModuleLightSection(PoleIndex, HeadIndex, ModuleIndex)
				]
			]

			// — Offset Location
			// per axis —
			+ SVerticalBox::Slot()
			.AutoHeight()
			.Padding(0, 2)
			[SNew(STextBlock)
				.Text(FText::FromString("Offset Location"))
			]
			// X axis
			+ SVerticalBox::Slot()
			.AutoHeight()
			.Padding(0, 2)
			[SNew(SHorizontalBox)
				// Label X
				+ SHorizontalBox::Slot()
				.AutoWidth()
				.VAlign(VAlign_Center)
				.Padding(0, 0, 8, 0)
				[SNew(STextBlock)
					.Text(FText::FromString("X"))
				]
				// Numeric Entry
				+ SHorizontalBox::Slot()
				.FillWidth(0.3f)
				.Padding(2, 0)
				[SNew(SNumericEntryBox<float>)
					.Value_Lambda([this, PoleIndex, HeadIndex, ModuleIndex]() -> TOptional<float>
					{
						if (!Poles.IsValidIndex(PoleIndex) || !Poles[PoleIndex].Heads.IsValidIndex(HeadIndex) ||
							!Poles[PoleIndex].Heads[HeadIndex].Modules.IsValidIndex(ModuleIndex))
						{
							return 0.0f;
						}
						return static_cast<float>(Poles[PoleIndex].Heads[HeadIndex].Modules[ModuleIndex].Offset.GetLocation().X);
					})
					.OnValueCommitted_Lambda([this, PoleIndex, HeadIndex, ModuleIndex](float V, ETextCommit::Type)
					{
						if (!Poles.IsValidIndex(PoleIndex) || !Poles[PoleIndex].Heads.IsValidIndex(HeadIndex) ||
							!Poles[PoleIndex].Heads[HeadIndex].Modules.IsValidIndex(ModuleIndex))
						{
							return;
						}
						FTLModule& Module{Poles[PoleIndex].Heads[HeadIndex].Modules[ModuleIndex]};
						FVector L{Module.Offset.GetLocation()};
						L.X = V;
						Module.Offset.SetLocation(L);
						Rebuild();
					})
				]
			]
			// Y axis
			+ SVerticalBox::Slot()
			.AutoHeight()
			.Padding(0, 2)
			[SNew(SHorizontalBox)
				+ SHorizontalBox::Slot()
				.AutoWidth()
				.VAlign(VAlign_Center)
				.Padding(0, 0, 8, 0)
				[SNew(STextBlock)
					.Text(FText::FromString("Y"))
				] +
				SHorizontalBox::Slot()
				.FillWidth(0.3f)
				.Padding(2, 0)
				[SNew(SNumericEntryBox<float>)
					.Value_Lambda([this, PoleIndex, HeadIndex, ModuleIndex]() -> TOptional<float>
					{
						if (!Poles.IsValidIndex(PoleIndex) || !Poles[PoleIndex].Heads.IsValidIndex(HeadIndex) ||
							!Poles[PoleIndex].Heads[HeadIndex].Modules.IsValidIndex(ModuleIndex))
						{
							return 0.0f;
						}
						return static_cast<float>(Poles[PoleIndex].Heads[HeadIndex].Modules[ModuleIndex].Offset.GetLocation().Y);
					})
					.OnValueCommitted_Lambda([this, PoleIndex, HeadIndex, ModuleIndex](float V, ETextCommit::Type)
					{
						if (!Poles.IsValidIndex(PoleIndex) || !Poles[PoleIndex].Heads.IsValidIndex(HeadIndex) ||
							!Poles[PoleIndex].Heads[HeadIndex].Modules.IsValidIndex(ModuleIndex))
						{
							return;
						}
						FTLModule& Module{Poles[PoleIndex].Heads[HeadIndex].Modules[ModuleIndex]};
						FVector L{Module.Offset.GetLocation()};
						L.Y = V;
						Module.Offset.SetLocation(L);
						Rebuild();
					})
				]
			]
			// Z axis
			+ SVerticalBox::Slot()
			.AutoHeight()
			.Padding(0, 2)
			[SNew(SHorizontalBox)
				+ SHorizontalBox::Slot()
				.AutoWidth()
				.VAlign(VAlign_Center)
				.Padding(0, 0, 8, 0)
				[SNew(STextBlock)
					.Text(FText::FromString("Z"))
				] +
				SHorizontalBox::Slot()
				.FillWidth(0.3f)
				.Padding(2, 0)
				[SNew(SNumericEntryBox<float>)
					.Value_Lambda([this, PoleIndex, HeadIndex, ModuleIndex]() -> TOptional<float>
					{
						if (!Poles.IsValidIndex(PoleIndex) || !Poles[PoleIndex].Heads.IsValidIndex(HeadIndex) ||
							!Poles[PoleIndex].Heads[HeadIndex].Modules.IsValidIndex(ModuleIndex))
						{
							return 0.0f;
						}
						return static_cast<float>(Poles[PoleIndex].Heads[HeadIndex].Modules[ModuleIndex].Offset.GetLocation().Z);
					})
					.OnValueCommitted_Lambda([this, PoleIndex, HeadIndex, ModuleIndex](float V, ETextCommit::Type)
					{
						if (!Poles.IsValidIndex(PoleIndex) || !Poles[PoleIndex].Heads.IsValidIndex(HeadIndex) ||
							!Poles[PoleIndex].Heads[HeadIndex].Modules.IsValidIndex(ModuleIndex))
						{
							return;
						}
						FTLModule& Module{Poles[PoleIndex].Heads[HeadIndex].Modules[ModuleIndex]};
						FVector L{Module.Offset.GetLocation()};
						L.Z = V;
						Module.Offset.SetLocation(L);
						Rebuild();
					})
				]
			]
			//----------------------------------------------------------------
			// Offset Rotation (Pitch, Yaw, Roll)
			//----------------------------------------------------------------
			+ SVerticalBox::Slot()
			.AutoHeight()
			.Padding(0, 2)
			[SNew(STextBlock)
				.Text(FText::FromString("Offset Rotation"))
			]
			+ SVerticalBox::Slot()
			.AutoHeight()
			.Padding(0, 2)
			[SNew(SHorizontalBox)
				// Pitch
				+ SHorizontalBox::Slot()
				.FillWidth(1)
				.Padding(2, 0)
				[SNew(SNumericEntryBox<float>)
					.Value_Lambda([this, PoleIndex, HeadIndex, ModuleIndex]() -> TOptional<float>
					{
						if (!Poles.IsValidIndex(PoleIndex) || !Poles[PoleIndex].Heads.IsValidIndex(HeadIndex) ||
							!Poles[PoleIndex].Heads[HeadIndex].Modules.IsValidIndex(ModuleIndex))
						{
							return 0.0f;
						}
						return static_cast<float>(Poles[PoleIndex].Heads[HeadIndex].Modules[ModuleIndex].Offset.Rotator().Pitch);
					})
					.OnValueCommitted_Lambda([this, PoleIndex, HeadIndex, ModuleIndex](float V, ETextCommit::Type)
					{
						if (!Poles.IsValidIndex(PoleIndex) || !Poles[PoleIndex].Heads.IsValidIndex(HeadIndex) ||
							!Poles[PoleIndex].Heads[HeadIndex].Modules.IsValidIndex(ModuleIndex))
						{
							return;
						}
						FTLModule& Module{Poles[PoleIndex].Heads[HeadIndex].Modules[ModuleIndex]};
						FRotator R{Module.Offset.Rotator()};
						R.Pitch = V;
						Module.Offset.SetRotation(FQuat(R));
						Rebuild();
					})
				]
				// Yaw
				+ SHorizontalBox::Slot()
				.FillWidth(1)
				.Padding(2, 0)
				[SNew(SNumericEntryBox<float>)
					.Value_Lambda([this, PoleIndex, HeadIndex, ModuleIndex]() -> TOptional<float>
					{
						if (!Poles.IsValidIndex(PoleIndex) || !Poles[PoleIndex].Heads.IsValidIndex(HeadIndex) ||
							!Poles[PoleIndex].Heads[HeadIndex].Modules.IsValidIndex(ModuleIndex))
						{
							return 0.0f;
						}
						return static_cast<float>(Poles[PoleIndex].Heads[HeadIndex].Modules[ModuleIndex].Offset.Rotator().Yaw);
					})
					.OnValueCommitted_Lambda([this, PoleIndex, HeadIndex, ModuleIndex](float V, ETextCommit::Type)
					{
						if (!Poles.IsValidIndex(PoleIndex) || !Poles[PoleIndex].Heads.IsValidIndex(HeadIndex) ||
							!Poles[PoleIndex].Heads[HeadIndex].Modules.IsValidIndex(ModuleIndex))
						{
							return;
						}
						FTLModule& Module{Poles[PoleIndex].Heads[HeadIndex].Modules[ModuleIndex]};
						FRotator R{Module.Offset.Rotator()};
						R.Yaw = V;
						Module.Offset.SetRotation(FQuat(R));
						Rebuild();
					})
				]
				// Roll
				+ SHorizontalBox::Slot()
				.FillWidth(1)
				.Padding(2, 0)
				[SNew(SNumericEntryBox<float>)
					.Value_Lambda([this, PoleIndex, HeadIndex, ModuleIndex]() -> TOptional<float>
					{
						if (!Poles.IsValidIndex(PoleIndex) || !Poles[PoleIndex].Heads.IsValidIndex(HeadIndex) ||
							!Poles[PoleIndex].Heads[HeadIndex].Modules.IsValidIndex(ModuleIndex))
						{
							return 0.0f;
						}
						return static_cast<float>(Poles[PoleIndex].Heads[HeadIndex].Modules[ModuleIndex].Offset.Rotator().Roll);
					})
					.OnValueCommitted_Lambda([this, PoleIndex, HeadIndex, ModuleIndex](float V, ETextCommit::Type)
					{
						if (!Poles.IsValidIndex(PoleIndex) || !Poles[PoleIndex].Heads.IsValidIndex(HeadIndex) ||
							!Poles[PoleIndex].Heads[HeadIndex].Modules.IsValidIndex(ModuleIndex))
						{
							return;
						}
						FTLModule& Module{Poles[PoleIndex].Heads[HeadIndex].Modules[ModuleIndex]};
						FRotator R{Module.Offset.Rotator()};
						R.Roll = V;
						Module.Offset.SetRotation(FQuat(R));
						Rebuild();
					})
				]
			]
			// — Delete Module button —
			+ SVerticalBox::Slot()
			.AutoHeight()
			.Padding(0, 2)
			[SNew(SButton)
				.Text(FText::FromString("Delete Module"))
				.OnClicked(this, &STrafficLightToolWidget::OnDeleteModuleClicked, PoleIndex, HeadIndex, ModuleIndex)
			]
		]
	];

	// clang-format on
}

TSharedRef<SWidget> STrafficLightToolWidget::BuildModuleSection(int32 PoleIndex, int32 HeadIndex)
{
	if (!EditorPoles.IsValidIndex(PoleIndex) || !Poles.IsValidIndex(PoleIndex) ||
		!EditorPoles[PoleIndex].Heads.IsValidIndex(HeadIndex) || !Poles[PoleIndex].Heads.IsValidIndex(HeadIndex))
	{
		return SNullWidget::NullWidget;
	}
	FEditorHead& EditorHead{EditorPoles[PoleIndex].Heads[HeadIndex]};

	TSharedRef<SVerticalBox> Container{SNew(SVerticalBox)};

	// clang-format off
	const int32 NumModules{FMath::Min(EditorHead.Modules.Num(), Poles[PoleIndex].Heads[HeadIndex].Modules.Num())};
	for (int32 ModuleIndex{0}; ModuleIndex < NumModules; ++ModuleIndex)
	{
		FEditorModule& EditorModule{EditorHead.Modules[ModuleIndex]};

		Container->AddSlot()
		.AutoHeight()
		.Padding(4, 2)
		[SNew(SExpandableArea)
			.InitiallyCollapsed(!EditorModule.bExpanded)
			.AreaTitle(FText::FromString(FString::Printf(TEXT("Module %d"), ModuleIndex)))
			.Padding(4)
			.OnAreaExpansionChanged_Lambda([this, PoleIndex, HeadIndex, ModuleIndex](bool bExpanded)
			{
				if (EditorPoles.IsValidIndex(PoleIndex) && EditorPoles[PoleIndex].Heads.IsValidIndex(HeadIndex) &&
					EditorPoles[PoleIndex].Heads[HeadIndex].Modules.IsValidIndex(ModuleIndex))
				{
					EditorPoles[PoleIndex].Heads[HeadIndex].Modules[ModuleIndex].bExpanded = bExpanded;
				}
			})
			.BodyContent()[BuildModuleEntry(PoleIndex, HeadIndex, ModuleIndex)]];
	}

	return Container;
	// clang-format on
}

TSharedRef<SWidget> STrafficLightToolWidget::BuildModuleLightEntry(
	int32 PoleIndex, int32 HeadIndex, int32 ModuleIndex, int32 LightIndex)
{
	if (!EditorPoles.IsValidIndex(PoleIndex) || !Poles.IsValidIndex(PoleIndex) ||
		!EditorPoles[PoleIndex].Heads.IsValidIndex(HeadIndex) || !Poles[PoleIndex].Heads.IsValidIndex(HeadIndex) ||
		!EditorPoles[PoleIndex].Heads[HeadIndex].Modules.IsValidIndex(ModuleIndex) ||
		!Poles[PoleIndex].Heads[HeadIndex].Modules.IsValidIndex(ModuleIndex) ||
		!Poles[PoleIndex].Heads[HeadIndex].Modules[ModuleIndex].Lights.IsValidIndex(LightIndex))
	{
		return SNullWidget::NullWidget;
	}
	FTLModule& Module{Poles[PoleIndex].Heads[HeadIndex].Modules[ModuleIndex]};
	const FTLModuleLight& ModuleLight{Module.Lights[LightIndex]};

	const int32 InitialLightTypeIdx{static_cast<int32>(ModuleLight.LightType)};
	const TSharedPtr<FString> InitialLightTypeItem{
		LightTypeOptions.IsValidIndex(InitialLightTypeIdx) ? LightTypeOptions[InitialLightTypeIdx] : TSharedPtr<FString>()};

	// clang-format off
	return SNew(SVerticalBox)
	+ SVerticalBox::Slot()
		.AutoHeight()
		.Padding(2, 0)
		[
		SNew(SHorizontalBox)

		// — Light-Type Combo —
		+ SHorizontalBox::Slot()
			.AutoWidth()
			.Padding(2, 0)
			[
			SNew(SComboBox<TSharedPtr<FString>>)
				.OptionsSource(&LightTypeOptions)
				.InitiallySelectedItem(InitialLightTypeItem)
				.OnGenerateWidget_Lambda([](TSharedPtr<FString> In)
				{
					return SNew(STextBlock).Text(FText::FromString(In.IsValid() ? *In : FString()));
				})
				.OnSelectionChanged_Lambda([this, PoleIndex, HeadIndex, ModuleIndex, LightIndex](TSharedPtr<FString> NewSelection, ESelectInfo::Type)
				{
					if (!NewSelection.IsValid() || !Poles.IsValidIndex(PoleIndex) ||
						!Poles[PoleIndex].Heads.IsValidIndex(HeadIndex) ||
						!Poles[PoleIndex].Heads[HeadIndex].Modules.IsValidIndex(ModuleIndex) ||
						!Poles[PoleIndex].Heads[HeadIndex].Modules[ModuleIndex].Lights.IsValidIndex(LightIndex))
					{
						return;
					}
					const int32 Choice{LightTypeOptions.IndexOfByPredicate(
						[&](const auto& Ptr) { return Ptr.IsValid() && *Ptr == *NewSelection; })};
					if (Choice == INDEX_NONE)
					{
						return;
					}
					FTLModuleLight& Light{Poles[PoleIndex].Heads[HeadIndex].Modules[ModuleIndex].Lights[LightIndex]};
					Light.LightType = static_cast<ETLLightType>(Choice);
					InitializeDefaultLight(Light);
					Rebuild();
				})
			[
				SNew(STextBlock)
				.Text_Lambda([this, PoleIndex, HeadIndex, ModuleIndex, LightIndex]()
				{
					if (!Poles.IsValidIndex(PoleIndex) || !Poles[PoleIndex].Heads.IsValidIndex(HeadIndex) ||
						!Poles[PoleIndex].Heads[HeadIndex].Modules.IsValidIndex(ModuleIndex) ||
						!Poles[PoleIndex].Heads[HeadIndex].Modules[ModuleIndex].Lights.IsValidIndex(LightIndex))
					{
						return FText::GetEmpty();
					}
					const int32 Idx{static_cast<int32>(
						Poles[PoleIndex].Heads[HeadIndex].Modules[ModuleIndex].Lights[LightIndex].LightType)};
					return FText::FromString(
						(LightTypeOptions.IsValidIndex(Idx) && LightTypeOptions[Idx].IsValid()) ? *LightTypeOptions[Idx] : FString());
				})
			]
			]

		// — Emissive-Intensity Slider —
		+ SHorizontalBox::Slot()
			.FillWidth(1.0f)
			.Padding(2, 0)
			[
			SNew(SSlider)
				.MinValue(0.0f)
				.MaxValue(100000.0f)
				.Value_Lambda([this, PoleIndex, HeadIndex, ModuleIndex, LightIndex]()
				{
					if (!Poles.IsValidIndex(PoleIndex) || !Poles[PoleIndex].Heads.IsValidIndex(HeadIndex) ||
						!Poles[PoleIndex].Heads[HeadIndex].Modules.IsValidIndex(ModuleIndex) ||
						!Poles[PoleIndex].Heads[HeadIndex].Modules[ModuleIndex].Lights.IsValidIndex(LightIndex))
					{
						return 0.0f;
					}
					return Poles[PoleIndex].Heads[HeadIndex].Modules[ModuleIndex].Lights[LightIndex].EmissiveIntensity;
				})
				.OnValueChanged_Lambda([this, PoleIndex, HeadIndex, ModuleIndex, LightIndex](float NewVal)
				{
					if (!Poles.IsValidIndex(PoleIndex) || !Poles[PoleIndex].Heads.IsValidIndex(HeadIndex) ||
						!Poles[PoleIndex].Heads[HeadIndex].Modules.IsValidIndex(ModuleIndex) ||
						!Poles[PoleIndex].Heads[HeadIndex].Modules[ModuleIndex].Lights.IsValidIndex(LightIndex))
					{
						return;
					}
					Poles[PoleIndex].Heads[HeadIndex].Modules[ModuleIndex].Lights[LightIndex].EmissiveIntensity = NewVal;
					UpdatePreviewLightParameter(PoleIndex, HeadIndex, ModuleIndex, LightIndex);
				})
			]
		]

	+ SVerticalBox::Slot()
	.AutoHeight()
	.Padding(2, 4)
	[
		SNew(SColorPicker)
		.TargetColorAttribute_Lambda([this, PoleIndex, HeadIndex, ModuleIndex, LightIndex]()
		{
			if (!Poles.IsValidIndex(PoleIndex) || !Poles[PoleIndex].Heads.IsValidIndex(HeadIndex) ||
				!Poles[PoleIndex].Heads[HeadIndex].Modules.IsValidIndex(ModuleIndex) ||
				!Poles[PoleIndex].Heads[HeadIndex].Modules[ModuleIndex].Lights.IsValidIndex(LightIndex))
			{
				return FLinearColor::Black;
			}
			return Poles[PoleIndex].Heads[HeadIndex].Modules[ModuleIndex].Lights[LightIndex].EmissiveColor;
		})
		.UseAlpha(false)
		.OnlyRefreshOnMouseUp(false)
		.OnColorCommitted_Lambda([this, PoleIndex, HeadIndex, ModuleIndex, LightIndex](FLinearColor NewColor)
		{
			if (!Poles.IsValidIndex(PoleIndex) || !Poles[PoleIndex].Heads.IsValidIndex(HeadIndex) ||
				!Poles[PoleIndex].Heads[HeadIndex].Modules.IsValidIndex(ModuleIndex) ||
				!Poles[PoleIndex].Heads[HeadIndex].Modules[ModuleIndex].Lights.IsValidIndex(LightIndex))
			{
				return;
			}
			Poles[PoleIndex].Heads[HeadIndex].Modules[ModuleIndex].Lights[LightIndex].EmissiveColor = NewColor;
			UpdatePreviewLightParameter(PoleIndex, HeadIndex, ModuleIndex, LightIndex);
		})
	];
	// clang-format on
}

TSharedRef<SWidget> STrafficLightToolWidget::BuildModuleLightSection(int32 PoleIndex, int32 HeadIndex, int32 ModuleIndex)
{
	if (!EditorPoles.IsValidIndex(PoleIndex) || !Poles.IsValidIndex(PoleIndex) ||
		!EditorPoles[PoleIndex].Heads.IsValidIndex(HeadIndex) || !Poles[PoleIndex].Heads.IsValidIndex(HeadIndex) ||
		!EditorPoles[PoleIndex].Heads[HeadIndex].Modules.IsValidIndex(ModuleIndex) ||
		!Poles[PoleIndex].Heads[HeadIndex].Modules.IsValidIndex(ModuleIndex))
	{
		return SNullWidget::NullWidget;
	}
	FEditorModule& EditorModule{EditorPoles[PoleIndex].Heads[HeadIndex].Modules[ModuleIndex]};
	FTLModule& Module{Poles[PoleIndex].Heads[HeadIndex].Modules[ModuleIndex]};

	TArray<FEditorModuleLight>& EditorLightsArray{EditorModule.Lights};
	TSharedRef<SVerticalBox> Container{SNew(SVerticalBox)};

	// clang-format off
	const int32 NumLights{FMath::Min(EditorLightsArray.Num(), Module.Lights.Num())};
	for (int32 LightIndex{0}; LightIndex < NumLights; ++LightIndex)
	{
		FEditorModuleLight& EditorLight{EditorLightsArray[LightIndex]};
		Container->AddSlot()
		.AutoHeight()
		.Padding(4, 2)
		[SNew(SExpandableArea)
			.InitiallyCollapsed(!EditorLight.bExpanded)
			.AreaTitle(FText::FromString(FString::Printf(TEXT("Light %d"), LightIndex)))
			.Padding(4)
			.OnAreaExpansionChanged_Lambda([this, PoleIndex, HeadIndex, ModuleIndex, LightIndex](bool bExpanded)
			{
				if (EditorPoles.IsValidIndex(PoleIndex) && EditorPoles[PoleIndex].Heads.IsValidIndex(HeadIndex) &&
					EditorPoles[PoleIndex].Heads[HeadIndex].Modules.IsValidIndex(ModuleIndex) &&
					EditorPoles[PoleIndex].Heads[HeadIndex].Modules[ModuleIndex].Lights.IsValidIndex(LightIndex))
				{
					EditorPoles[PoleIndex].Heads[HeadIndex].Modules[ModuleIndex].Lights[LightIndex].bExpanded = bExpanded;
				}
			})
			.BodyContent()[BuildModuleLightEntry(PoleIndex, HeadIndex, ModuleIndex, LightIndex)]];
	}

	return Container;
	// clang-format on
}

// ----------------------------------------------------------------------------------------------------------------
// ******************************   ENUM TO TEXT
// ----------------------------------------------------------------------------------------------------------------
FString STrafficLightToolWidget::GetHeadStyleText(ETLStyle Style)
{
	const UEnum* EnumPtr{StaticEnum<ETLStyle>()};
	if (!IsValid(EnumPtr))
	{
		return FString(TEXT("Unknown"));
	}
	return EnumPtr->GetDisplayNameTextByValue((int64) Style).ToString();
}

FString STrafficLightToolWidget::GetHeadAttachmentText(ETLHeadAttachment Attach)
{
	const UEnum* EnumPtr{StaticEnum<ETLHeadAttachment>()};
	if (!IsValid(EnumPtr))
	{
		return FString(TEXT("Unknown"));
	}
	return EnumPtr->GetDisplayNameTextByValue((int64) Attach).ToString();
}

FString STrafficLightToolWidget::GetHeadOrientationText(ETLOrientation Orient)
{
	const UEnum* EnumPtr{StaticEnum<ETLOrientation>()};
	if (!IsValid(EnumPtr))
	{
		return FString(TEXT("Unknown"));
	}
	return EnumPtr->GetDisplayNameTextByValue((int64) Orient).ToString();
}

FString STrafficLightToolWidget::GetLightTypeText(ETLLightType Type)
{
	const UEnum* EnumPtr{StaticEnum<ETLLightType>()};
	if (!IsValid(EnumPtr))
	{
		return FString(TEXT("Unknown"));
	}
	return EnumPtr->GetDisplayNameTextByValue((int64) Type).ToString();
}

EVisibility STrafficLightToolWidget::GetModifyVisibility() const
{
	if (!GEditor)
	{
		return EVisibility::Collapsed;
	}

	UWorld* const EditorWorld{GEditor->GetEditorWorldContext().World()};
	if (!EditorWorld)
	{
		return EVisibility::Collapsed;
	}

	int32 SelectedCount{0};
	AActor* SelectedActor{nullptr};
	for (FSelectionIterator It{GEditor->GetSelectedActorIterator()}; It; ++It)
	{
		AActor* const Actor{Cast<AActor>(*It)};
		if (IsValid(Actor))
		{
			SelectedActor = Actor;
			++SelectedCount;
		}
	}

	if (SelectedCount != 1)
	{
		return EVisibility::Collapsed;
	}

	USceneComponent* const Root{SelectedActor->GetRootComponent()};
	if (!(IsValid(Root) && Root->GetName() == TEXT("BakedRoot")))
	{
		return EVisibility::Collapsed;
	}

	return EVisibility::Visible;
}
