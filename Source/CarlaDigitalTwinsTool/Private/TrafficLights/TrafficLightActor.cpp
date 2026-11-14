#include "TrafficLights/TrafficLightActor.h"

#include "BlueprintUtil/BlueprintUtilFunctions.h"
#include "CarlaDigitalTwinsTool.h"
#include "Components/SceneComponent.h"
#include "Components/StaticMeshComponent.h"
#include "Containers/UnrealString.h"
#include "Dom/JsonObject.h"
#include "Editor.h"
#include "Engine/StaticMesh.h"
#include "Engine/StaticMeshActor.h"
#include "Engine/StaticMeshSocket.h"
#include "Generation/MapGenFunctionLibrary.h"
#include "JsonUtilities.h"
#include "Logging/LogMacros.h"
#include "Logging/LogVerbosity.h"
#include "Materials/MaterialInstanceDynamic.h"
#include "Math/MathFwd.h"
#include "Misc/FileHelper.h"
#include "Misc/Paths.h"
#include "Serialization/JsonReader.h"
#include "Serialization/JsonSerializer.h"
#include "Serialization/JsonWriter.h"
#include "Templates/SharedPointer.h"
#include "TimerManager.h"
#include "TrafficLights/TLHead.h"
#include "TrafficLights/TLMaterialFactory.h"
#include "TrafficLights/TLMeshFactory.h"
#include "TrafficLights/TLModule.h"
#include "TrafficLights/TLPole.h"
#include "UObject/Object.h"

namespace
{
double GetJsonNumberOrDefault(const TSharedPtr<FJsonObject>& Obj, const TCHAR* Field, double DefaultVal = 0.0)
{
	if (!Obj.IsValid())
	{
		return DefaultVal;
	}
	double OutVal = DefaultVal;
	return Obj->TryGetNumberField(Field, OutVal) ? OutVal : DefaultVal;
}

FTransform ParseTransform(const TSharedPtr<FJsonObject>& Json)
{
	if (!Json.IsValid())
	{
		return FTransform::Identity;
	}

	FVector Location = FVector::ZeroVector;
	FRotator Rotation = FRotator::ZeroRotator;
	FVector Scale3D = FVector::OneVector;

	const TSharedPtr<FJsonObject>* Loc = nullptr;
	if (Json->TryGetObjectField(TEXT("Location"), Loc) && Loc && Loc->IsValid())
	{
		Location = FVector(
			GetJsonNumberOrDefault(*Loc, TEXT("X"), 0.0),
			GetJsonNumberOrDefault(*Loc, TEXT("Y"), 0.0),
			GetJsonNumberOrDefault(*Loc, TEXT("Z"), 0.0));
	}

	const TSharedPtr<FJsonObject>* Rot = nullptr;
	if (Json->TryGetObjectField(TEXT("Rotation"), Rot) && Rot && Rot->IsValid())
	{
		Rotation = FRotator(
			GetJsonNumberOrDefault(*Rot, TEXT("Pitch"), 0.0),
			GetJsonNumberOrDefault(*Rot, TEXT("Yaw"), 0.0),
			GetJsonNumberOrDefault(*Rot, TEXT("Roll"), 0.0));
	}

	const TSharedPtr<FJsonObject>* Scale = nullptr;
	if (Json->TryGetObjectField(TEXT("Scale"), Scale) && Scale && Scale->IsValid())
	{
		Scale3D = FVector(
			GetJsonNumberOrDefault(*Scale, TEXT("X"), 1.0),
			GetJsonNumberOrDefault(*Scale, TEXT("Y"), 1.0),
			GetJsonNumberOrDefault(*Scale, TEXT("Z"), 1.0));
	}

	return FTransform(Rotation, Location, Scale3D);
}

template <typename TEnum>
TEnum GetEnumValueFromString(const FString& Name)
{
	if (UEnum* Enum = StaticEnum<TEnum>())
	{
		int64 Value = Enum->GetValueByNameString(Name);
		if (Value != INDEX_NONE)
		{
			return static_cast<TEnum>(Value);
		}
		UE_LOG(LogCarlaDigitalTwinsTool, Warning, TEXT("Invalid enum name '%s' for %s"), *Name, *Enum->GetName());
	}
	UE_LOG(LogCarlaDigitalTwinsTool, Warning, TEXT("Enum not found"));
	return TEnum(0);
}

TSharedPtr<FJsonObject> TransformToJson(const FTransform& T)
{
	TSharedPtr<FJsonObject> J = MakeShared<FJsonObject>();
	{
		FVector L = T.GetLocation();
		TSharedPtr<FJsonObject> JL = MakeShared<FJsonObject>();
		JL->SetNumberField(TEXT("X"), L.X);
		JL->SetNumberField(TEXT("Y"), L.Y);
		JL->SetNumberField(TEXT("Z"), L.Z);
		J->SetObjectField(TEXT("Location"), JL);
	}
	{
		FRotator R = T.Rotator();
		TSharedPtr<FJsonObject> JR = MakeShared<FJsonObject>();
		JR->SetNumberField(TEXT("Pitch"), R.Pitch);
		JR->SetNumberField(TEXT("Yaw"), R.Yaw);
		JR->SetNumberField(TEXT("Roll"), R.Roll);
		J->SetObjectField(TEXT("Rotation"), JR);
	}
	{
		FVector S = T.GetScale3D();
		TSharedPtr<FJsonObject> JS = MakeShared<FJsonObject>();
		JS->SetNumberField(TEXT("X"), S.X);
		JS->SetNumberField(TEXT("Y"), S.Y);
		JS->SetNumberField(TEXT("Z"), S.Z);
		J->SetObjectField(TEXT("Scale"), JS);
	}
	return J;
}

template <typename TEnum>
FString EnumToString(TEnum Value)
{
	const UEnum* EnumPtr = StaticEnum<TEnum>();
	return EnumPtr ? EnumPtr->GetNameStringByValue(static_cast<int64>(Value)) : FString();
}

FString MakeUniqueNamed(const FString& Base)
{
	const FGuid G{FGuid::NewGuid()};
	return FString::Printf(TEXT("%s_%s"), *Base, *G.ToString(EGuidFormats::Short));
}

bool ShouldUseRerun(const AActor* Actor)
{
	if (!Actor)
	{
		return false;
	}
	const UWorld* World{Actor->GetWorld()};
	const bool bPlacedInEditor{World && (World->WorldType == EWorldType::Editor || World->WorldType == EWorldType::PIE)};
	const bool bPreview{Actor->HasAnyFlags(RF_Transient) || (World && World->WorldType == EWorldType::EditorPreview)};
	return bPlacedInEditor && !bPreview;
}

}	 // namespace

ATrafficLightActor::ATrafficLightActor()
{
	USceneComponent* const PolesRoot{CreateDefaultSubobject<USceneComponent>(TEXT("Poles"))};
	SetRootComponent(PolesRoot);
}

void ATrafficLightActor::OnConstruction(const FTransform& Transform)
{
	Super::OnConstruction(Transform);
	Build();
}

void ATrafficLightActor::EndPlay(const EEndPlayReason::Type EndPlayReason)
{
	StopDemoSequence();
	Super::EndPlay(EndPlayReason);
}

void ATrafficLightActor::Bake(const FString& MapName, const FString& DesiredLabel)
{
#if WITH_EDITOR
	UWorld* const EditorWorld{GetWorld()};
	if (!(EditorWorld && EditorWorld->IsEditorWorld()))
	{
		return;
	}
	if (MapName.IsEmpty() || MapName.Contains(TEXT("..")) || MapName.Contains(TEXT("/")) || MapName.Contains(TEXT("\\")))
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("Bake: Invalid MapName '%s'"), *MapName);
		return;
	}

	const FTransform ActorWorldTransform{GetActorTransform()};

	FActorSpawnParameters SpawnParams{};
	SpawnParams.SpawnCollisionHandlingOverride = ESpawnActorCollisionHandlingMethod::AlwaysSpawn;
	SpawnParams.ObjectFlags |= RF_Transactional;

	AActor* const ContainerActor{EditorWorld->SpawnActor<AActor>(AActor::StaticClass(), FTransform::Identity, SpawnParams)};
	if (!IsValid(ContainerActor))
	{
		return;
	}

	ContainerActor->SetActorLabel(DesiredLabel);
	ContainerActor->Tags.AddUnique(FName(*DesiredLabel));
	ContainerActor->SetFolderPath(FName(TEXT("TrafficLights")));
	ContainerActor->SetIsTemporarilyHiddenInEditor(false);
#if ENGINE_MAJOR_VERSION >= 5
	ContainerActor->SetIsSpatiallyLoaded(false);
#endif

	USceneComponent* ContainerRoot{ContainerActor->GetRootComponent()};
	if (!IsValid(ContainerRoot))
	{
		USceneComponent* const BakedRoot{NewObject<USceneComponent>(ContainerActor, TEXT("BakedRoot"))};
		ContainerActor->SetRootComponent(BakedRoot);
		BakedRoot->RegisterComponent();
		ContainerRoot = BakedRoot;
	}
	ContainerRoot->SetWorldTransform(ActorWorldTransform);

	TArray<UStaticMeshComponent*> SourceMeshComponents{};
	GetComponents<UStaticMeshComponent>(SourceMeshComponents);

	TArray<UStaticMeshComponent*> BakedMeshComponents{};
	BakedMeshComponents.Reserve(SourceMeshComponents.Num());

	for (UStaticMeshComponent* const SourceMeshComponent : SourceMeshComponents)
	{
		if (!(IsValid(SourceMeshComponent) && IsValid(SourceMeshComponent->GetStaticMesh())))
		{
			continue;
		}

		const FTransform SourceWorldTransform{SourceMeshComponent->GetComponentTransform()};

		UStaticMeshComponent* const BakedMeshComponent{NewObject<UStaticMeshComponent>(ContainerActor)};
		if (!IsValid(BakedMeshComponent))
		{
			continue;
		}

		const FString SourceCompName = SourceMeshComponent->GetName();
		const FName SafeCompName = FindObjectFast<UObject>(ContainerActor, *SourceCompName)
			? MakeUniqueObjectName(ContainerActor, UStaticMeshComponent::StaticClass(), *SourceCompName)
			: FName(*SourceCompName);
		BakedMeshComponent->Rename(*SafeCompName.ToString(), ContainerActor, REN_DontCreateRedirectors);
		BakedMeshComponent->SetupAttachment(ContainerRoot);
		ContainerActor->AddInstanceComponent(BakedMeshComponent);
		BakedMeshComponent->RegisterComponent();

		BakedMeshComponent->SetStaticMesh(SourceMeshComponent->GetStaticMesh());

		const int32 MaterialCount{SourceMeshComponent->GetNumMaterials()};
		for (int32 MaterialIndex{0}; MaterialIndex < MaterialCount; ++MaterialIndex)
		{
			UMaterialInterface* const SourceMaterial{SourceMeshComponent->GetMaterial(MaterialIndex)};
			if (IsValid(SourceMaterial))
			{
				BakedMeshComponent->SetMaterial(MaterialIndex, SourceMaterial);
			}
		}

		BakedMeshComponent->SetWorldTransform(SourceWorldTransform);
		BakedMeshComponents.Add(BakedMeshComponent);
	}

	TMap<UObject*, UObject*> CopiedAssetsCache;
	auto CopyCached = [&](UObject* SourceAsset) -> UObject*
	{
		if (!IsValid(SourceAsset))
		{
			return nullptr;
		}
		if (UObject* const* Cached = CopiedAssetsCache.Find(SourceAsset))
		{
			return *Cached;
		}
		UObject* Copied = UBlueprintUtilFunctions::CopyAssetToPlugin(SourceAsset, MapName);
		CopiedAssetsCache.Add(SourceAsset, Copied);
		return Copied;
	};

	for (UStaticMeshComponent* const BakedMeshComponent : BakedMeshComponents)
	{
		UStaticMesh* const OriginalMesh{BakedMeshComponent->GetStaticMesh()};
		if (!IsValid(OriginalMesh))
		{
			continue;
		}

		UStaticMesh* const CopiedMesh{Cast<UStaticMesh>(CopyCached(OriginalMesh))};
		if (IsValid(CopiedMesh))
		{
			BakedMeshComponent->SetStaticMesh(CopiedMesh);
		}

		const TArray<FName> SlotNames{BakedMeshComponent->GetMaterialSlotNames()};
		const int32 MaterialCount{BakedMeshComponent->GetNumMaterials()};
		for (int32 MaterialIndex{0}; MaterialIndex < MaterialCount; ++MaterialIndex)
		{
			UMaterialInterface* const AssignedMaterial{BakedMeshComponent->GetMaterial(MaterialIndex)};
			if (!IsValid(AssignedMaterial))
			{
				continue;
			}

			if (UMaterialInstanceDynamic* const AssignedMID{Cast<UMaterialInstanceDynamic>(AssignedMaterial)})
			{
				UMaterialInterface* const ParentMat{AssignedMID->Parent};
				UObject* const CopiedParentObj{CopyCached(ParentMat)};
				UMaterialInterface* const ParentForNewMID{
					IsValid(CopiedParentObj) ? Cast<UMaterialInterface>(CopiedParentObj) : ParentMat};
				if (IsValid(ParentForNewMID))
				{
					UMaterialInstanceDynamic* const NewMID{UMaterialInstanceDynamic::Create(ParentForNewMID, ContainerActor)};
					if (IsValid(NewMID))
					{
						NewMID->K2_CopyMaterialInstanceParameters(AssignedMID);
						if (SlotNames.IsValidIndex(MaterialIndex) &&
							SlotNames[MaterialIndex].ToString().StartsWith(TEXT("led_")))
						{
							NewMID->SetScalarParameterValue(TEXT("Emissive Intensity"), 0.0f);
						}
						BakedMeshComponent->SetMaterial(MaterialIndex, NewMID);
					}
				}
				continue;
			}

			UObject* const CopiedMatObj{CopyCached(AssignedMaterial)};
			if (UMaterialInterface* const NewMat{Cast<UMaterialInterface>(CopiedMatObj)})
			{
				BakedMeshComponent->SetMaterial(MaterialIndex, NewMat);
			}
		}
	}

	// Export traffic light logic to map_logic.json in the generated plugin's OpenDrive folder
	FString PluginContentDir = FPaths::ProjectPluginsDir() / MapName / TEXT("Content/Maps/OpenDrive");
	FString MapLogicFilePath = PluginContentDir / TEXT("map_logic.json");

	// Ensure directory exists
	if (!FPlatformFileManager::Get().GetPlatformFile().DirectoryExists(*PluginContentDir))
	{
		FPlatformFileManager::Get().GetPlatformFile().CreateDirectoryTree(*PluginContentDir);
	}

	// Load existing map_logic.json or create new structure
	TSharedPtr<FJsonObject> MapLogicRoot;
	TArray<TSharedPtr<FJsonValue>> TrafficLightsArray;

	// Try to load existing file
	FString ExistingContent;
	if (FFileHelper::LoadFileToString(ExistingContent, *MapLogicFilePath))
	{
		TSharedRef<TJsonReader<>> Reader = TJsonReaderFactory<>::Create(ExistingContent);
		if (FJsonSerializer::Deserialize(Reader, MapLogicRoot) && MapLogicRoot.IsValid())
		{
			const TArray<TSharedPtr<FJsonValue>>* ExistingTrafficLights;
			if (MapLogicRoot->TryGetArrayField(TEXT("TrafficLights"), ExistingTrafficLights))
			{
				TrafficLightsArray = *ExistingTrafficLights;
			}
		}
	}

	if (!MapLogicRoot.IsValid())
	{
		MapLogicRoot = MakeShareable(new FJsonObject);
	}

	FString CurrentTrafficLightJSON = ExportLogicToJSON(DesiredLabel);
	TSharedPtr<FJsonObject> CurrentTrafficLightObj;
	TSharedRef<TJsonReader<>> TrafficLightReader = TJsonReaderFactory<>::Create(CurrentTrafficLightJSON);
	if (FJsonSerializer::Deserialize(TrafficLightReader, CurrentTrafficLightObj) && CurrentTrafficLightObj.IsValid())
	{
		TrafficLightsArray.RemoveAll([&DesiredLabel](const TSharedPtr<FJsonValue>& Value) {
			if (Value.IsValid() && Value->Type == EJson::Object)
			{
				TSharedPtr<FJsonObject> Obj = Value->AsObject();
				FString ActorName;
				if (Obj.IsValid() && Obj->TryGetStringField(TEXT("ActorName"), ActorName))
				{
					return ActorName == DesiredLabel;
				}
			}
			return false;
		});

		TrafficLightsArray.Add(MakeShareable(new FJsonValueObject(CurrentTrafficLightObj)));
	}

	MapLogicRoot->SetArrayField(TEXT("TrafficLights"), TrafficLightsArray);

	FString MapLogicOutput;
	TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&MapLogicOutput);
	if (FJsonSerializer::Serialize(MapLogicRoot.ToSharedRef(), Writer))
	{
		if (FFileHelper::SaveStringToFile(MapLogicOutput, *MapLogicFilePath))
		{
			UE_LOG(LogTemp, Log, TEXT("Traffic light logic added to map_logic.json: %s"), *MapLogicFilePath);
		}
		else
		{
			UE_LOG(LogTemp, Warning, TEXT("Failed to save map_logic.json: %s"), *MapLogicFilePath);
		}
	}

#endif
}

void ATrafficLightActor::Build()
{
	USceneComponent* const PolesRoot{EnsurePolesRoot()};
	Clear();

#if WITH_EDITOR
	UWorld* const World{GetWorld()};
	const bool bIsEditorWorld{World && (World->WorldType == EWorldType::Editor || World->WorldType == EWorldType::EditorPreview ||
										   World->WorldType == EWorldType::PIE)};
	const bool bIsPreview{HasAnyFlags(RF_Transient)};

	auto EnsureEditorInstanceComponent = [this, bIsEditorWorld, bIsPreview](UActorComponent* InComponent)
	{
		if (!IsValid(InComponent))
		{
			return;
		}
		if (!bIsEditorWorld)
		{
			return;
		}
		if (bIsPreview)
		{
			return;
		}

		InComponent->CreationMethod = EComponentCreationMethod::UserConstructionScript;
		InComponent->SetFlags(RF_Transactional);

		if (InComponent->GetOwner() != this)
		{
			AddInstanceComponent(InComponent);
		}

		if (!InComponent->IsRegistered())
		{
			InComponent->OnComponentCreated();
			InComponent->RegisterComponent();
		}
	};

	auto UniqueRename = [this](UObject* Object, const FString& BaseName)
	{
		if (!IsValid(Object))
		{
			return;
		}
		const FName Unique = FindObjectFast<UObject>(this, *BaseName)
			? MakeUniqueObjectName(this, Object->GetClass(), *BaseName)
			: FName(*BaseName);
		Object->Rename(*Unique.ToString(), this, REN_DontCreateRedirectors);
	};
#endif

	const int32 TotalPoles{Poles.Num()};
	for (int32 PoleIndex{0}; PoleIndex < TotalPoles; ++PoleIndex)
	{
		FTLPole& Pole{Poles[PoleIndex]};

		USceneComponent* const PoleRoot{AddRootPole(PolesRoot, Pole)};
#if WITH_EDITOR
		UniqueRename(PoleRoot, FString::Printf(TEXT("Pole_%02d"), PoleIndex));
		EnsureEditorInstanceComponent(PoleRoot);
#endif

		if (IsValid(Pole.BasePoleMesh))
		{
			UStaticMeshComponent* const BaseComp{AddPoleBase(PoleRoot, Pole)};
#if WITH_EDITOR
			UniqueRename(BaseComp, FString::Printf(TEXT("Pole_%02d_Base"), PoleIndex));
			EnsureEditorInstanceComponent(BaseComp);
#endif
		}

		if (IsValid(Pole.ExtensiblePoleMesh))
		{
			UStaticMeshComponent* const ExtComp{AddPoleExtensible(PoleRoot, Pole)};
#if WITH_EDITOR
			UniqueRename(ExtComp, FString::Printf(TEXT("Pole_%02d_Extensible"), PoleIndex));
			EnsureEditorInstanceComponent(ExtComp);
#endif
		}

		if (IsValid(Pole.CapPoleMesh))
		{
			UStaticMeshComponent* const CapComp{AddPoleCap(PoleRoot, Pole)};
#if WITH_EDITOR
			UniqueRename(CapComp, FString::Printf(TEXT("Pole_%02d_Cap"), PoleIndex));
			EnsureEditorInstanceComponent(CapComp);
#endif
		}

		const int32 TotalHeads{Pole.Heads.Num()};
		for (int32 HeadIndex{0}; HeadIndex < TotalHeads; ++HeadIndex)
		{
			FTLHead& Head{Pole.Heads[HeadIndex]};

			USceneComponent* const HeadRoot{AddHead(PoleRoot, Pole, Head)};
#if WITH_EDITOR
			UniqueRename(HeadRoot, FString::Printf(TEXT("Pole_%02d_Head_%02d"), PoleIndex, HeadIndex));
			EnsureEditorInstanceComponent(HeadRoot);
#endif

			const int32 TotalModules{Head.Modules.Num()};
			for (int32 ModuleIndex{0}; ModuleIndex < TotalModules; ++ModuleIndex)
			{
				FTLModule& Module{Head.Modules[ModuleIndex]};
				UStaticMeshComponent* const ModuleComp{AddModule(HeadRoot, Pole, Head, Module)};
				if (IsValid(ModuleComp))
				{
					ModuleMeshComponents.Add(ModuleComp);
#if WITH_EDITOR
					UniqueRename(
						ModuleComp, FString::Printf(TEXT("Pole_%02d_Head_%02d_Mod_%02d"), PoleIndex, HeadIndex, ModuleIndex));
					EnsureEditorInstanceComponent(ModuleComp);
#endif
				}
			}

			RebuildModuleChain(Head);

			if (Head.bHasBackplate)
			{
				TArray<UActorComponent*> Before{};
				GetComponents(UActorComponent::StaticClass(), Before);

				AddBackplate(HeadRoot, Pole, Head);

				TArray<UActorComponent*> After{};
				GetComponents(UActorComponent::StaticClass(), After);

				TSet<UActorComponent*> BeforeSet{};
				for (UActorComponent* const Existing : Before)
				{
					BeforeSet.Add(Existing);
				}

				int32 BackplateIndex{0};
				for (UActorComponent* const MaybeNew : After)
				{
					if (BeforeSet.Contains(MaybeNew))
					{
						continue;
					}
					USceneComponent* const SceneComp{Cast<USceneComponent>(MaybeNew)};
					if (!IsValid(SceneComp))
					{
						continue;
					}
					if (SceneComp->GetAttachParent() != HeadRoot)
					{
						continue;
					}
#if WITH_EDITOR
					UniqueRename(SceneComp,
						FString::Printf(TEXT("Pole_%02d_Head_%02d_Backplate_%02d"), PoleIndex, HeadIndex, BackplateIndex));
					EnsureEditorInstanceComponent(SceneComp);
#endif
					++BackplateIndex;
				}
			}
		}
	}

#if WITH_EDITOR
	if (GEditor && !HasAnyFlags(RF_Transient))
	{
		GEditor->NoteSelectionChange();
	}
#endif
}

void ATrafficLightActor::BuildFromJSON()
{
	if (JSONFile.FilePath.IsEmpty())
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("JSONFile was not assigned."));
		return;
	}

	const FString FullPath{FPaths::ConvertRelativePathToFull(JSONFile.FilePath)};
	FString JSONConfig;
	if (!FFileHelper::LoadFileToString(JSONConfig, *FullPath))
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("Failed to load JSON file: %s"), *FullPath);
		return;
	}
	BuildFromJSONString(JSONConfig);
	if (ShouldUseRerun(this))
	{
		RerunConstructionScripts();
	}
}

void ATrafficLightActor::BuildFromJSONString(const FString& JSONConfig)
{
	TSharedPtr<FJsonObject> Root;
	TSharedRef<TJsonReader<TCHAR>> Reader{TJsonReaderFactory<TCHAR>::Create(JSONConfig)};
	if (!FJsonSerializer::Deserialize(Reader, Root) || !Root.IsValid())
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("Failed to parse JSONConfig."));
		return;
	}

	{
		FTransform NewTx{GetActorTransform()};
		bool bHasWorldTransform = false;

		const TSharedPtr<FJsonObject>* PosObj = nullptr;
		if (Root->TryGetObjectField(TEXT("WorldPosition"), PosObj) && PosObj && PosObj->IsValid())
		{
			NewTx.SetLocation(FVector{
				GetJsonNumberOrDefault(*PosObj, TEXT("X"), 0.0),
				GetJsonNumberOrDefault(*PosObj, TEXT("Y"), 0.0),
				GetJsonNumberOrDefault(*PosObj, TEXT("Z"), 0.0)});
			bHasWorldTransform = true;
		}

		const TSharedPtr<FJsonObject>* RotObj = nullptr;
		if (Root->TryGetObjectField(TEXT("WorldRotation"), RotObj) && RotObj && RotObj->IsValid())
		{
			NewTx.SetRotation(FQuat(FRotator{
				GetJsonNumberOrDefault(*RotObj, TEXT("X"), 0.0),
				GetJsonNumberOrDefault(*RotObj, TEXT("Y"), 0.0),
				GetJsonNumberOrDefault(*RotObj, TEXT("Z"), 0.0)}));
			bHasWorldTransform = true;
		}
		const TSharedPtr<FJsonObject>* SclObj = nullptr;
		if (Root->TryGetObjectField(TEXT("WorldScale"), SclObj) && SclObj && SclObj->IsValid())
		{
			NewTx.SetScale3D(FVector{
				GetJsonNumberOrDefault(*SclObj, TEXT("X"), 1.0),
				GetJsonNumberOrDefault(*SclObj, TEXT("Y"), 1.0),
				GetJsonNumberOrDefault(*SclObj, TEXT("Z"), 1.0)});
			bHasWorldTransform = true;
		}

		if (bHasWorldTransform && !HasAnyFlags(RF_Transient))
		{
			SetActorTransform(NewTx);
		}
	}

	Poles.Empty();
	const TArray<TSharedPtr<FJsonValue>>* JsonPoles;
	if (Root->TryGetArrayField(TEXT("Poles"), JsonPoles))
	{
		for (const auto& PoleValue : *JsonPoles)
		{
			const TSharedPtr<FJsonObject> PoleObj{PoleValue.IsValid() ? PoleValue->AsObject() : nullptr};
			FTLPole Pole;
			if (!PoleObj.IsValid())
			{
				UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("Invalid Pole object in JSON."));
				continue;
			}
			if (PoleObj->HasTypedField<EJson::Object>(TEXT("Transform")))
			{
				Pole.Transform = ParseTransform(PoleObj->GetObjectField(TEXT("Transform")));
			}
			if (PoleObj->HasTypedField<EJson::Object>(TEXT("Offset")))
			{
				Pole.Offset = ParseTransform(PoleObj->GetObjectField(TEXT("Offset")));
			}
			if (PoleObj->HasTypedField<EJson::String>(TEXT("Style")))
			{
				Pole.Style = GetEnumValueFromString<ETLStyle>(PoleObj->GetStringField(TEXT("Style")));
			}
			if (PoleObj->HasTypedField<EJson::String>(TEXT("Orientation")))
			{
				Pole.Orientation = GetEnumValueFromString<ETLOrientation>(PoleObj->GetStringField(TEXT("Orientation")));
			}
			if (PoleObj->HasTypedField<EJson::Number>(TEXT("PoleHeight")))
			{
				Pole.PoleHeight = PoleObj->GetNumberField(TEXT("PoleHeight"));
			}
			if (PoleObj->HasTypedField<EJson::String>(TEXT("BaseMesh")))
			{
				const FString MeshName{PoleObj->GetStringField(TEXT("BaseMesh"))};
				UStaticMesh* Mesh{FTLMeshFactory::GetBaseMeshByName(MeshName)};
				if (IsValid(Mesh))
				{
					Pole.BasePoleMesh = Mesh;
				}
				else
				{
					UE_LOG(LogCarlaDigitalTwinsTool, Warning, TEXT("Base mesh '%s' not found, using default."), *MeshName);
				}
			}
			if (PoleObj->HasTypedField<EJson::String>(TEXT("ExtensibleMesh")))
			{
				const FString MeshName{PoleObj->GetStringField(TEXT("ExtensibleMesh"))};
				UStaticMesh* Mesh{FTLMeshFactory::GetExtensibleMeshByName(MeshName)};
				if (IsValid(Mesh))
				{
					Pole.ExtensiblePoleMesh = Mesh;
				}
				else
				{
					UE_LOG(LogCarlaDigitalTwinsTool, Warning, TEXT("Extensible mesh '%s' not found, using default."), *MeshName);
				}
			}
			if (PoleObj->HasTypedField<EJson::String>(TEXT("CapMesh")))
			{
				const FString MeshName{PoleObj->GetStringField(TEXT("CapMesh"))};
				UStaticMesh* Mesh{FTLMeshFactory::GetCapMeshByName(MeshName)};
				if (IsValid(Mesh))
				{
					Pole.CapPoleMesh = Mesh;
				}
				else
				{
					UE_LOG(LogCarlaDigitalTwinsTool, Warning, TEXT("Cap mesh '%s' not found, using default."), *MeshName);
				}
			}

			const TArray<TSharedPtr<FJsonValue>>* JsonHeads;
			if (PoleObj->TryGetArrayField(TEXT("Heads"), JsonHeads))
			{
				for (const auto& HeadValue : *JsonHeads)
				{
					const TSharedPtr<FJsonObject> HeadObj{HeadValue.IsValid() ? HeadValue->AsObject() : nullptr};
					FTLHead Head;
					if (!HeadObj.IsValid())
					{
						UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("Invalid Head object in JSON."));
						continue;
					}
					if (HeadObj->HasTypedField<EJson::Object>(TEXT("Transform")))
					{
						Head.Transform = ParseTransform(HeadObj->GetObjectField(TEXT("Transform")));
					}
					if (HeadObj->HasTypedField<EJson::Object>(TEXT("Offset")))
					{
						Head.Offset = ParseTransform(HeadObj->GetObjectField(TEXT("Offset")));
					}
					if (HeadObj->HasTypedField<EJson::String>(TEXT("Style")))
					{
						Head.Style = GetEnumValueFromString<ETLStyle>(HeadObj->GetStringField(TEXT("Style")));
					}
					if (HeadObj->HasTypedField<EJson::String>(TEXT("Attachment")))
					{
						Head.Attachment = GetEnumValueFromString<ETLHeadAttachment>(HeadObj->GetStringField(TEXT("Attachment")));
					}
					if (HeadObj->HasTypedField<EJson::String>(TEXT("Orientation")))
					{
						Head.Orientation = GetEnumValueFromString<ETLOrientation>(HeadObj->GetStringField(TEXT("Orientation")));
					}
					if (HeadObj->HasTypedField<EJson::Boolean>(TEXT("bHasBackplate")))
					{
						Head.bHasBackplate = HeadObj->GetBoolField(TEXT("bHasBackplate"));
					}
					if (HeadObj->HasTypedField<EJson::String>(TEXT("SignalID")))
					{
						Head.SignalID = HeadObj->GetStringField(TEXT("SignalID"));
					}

					const TArray<TSharedPtr<FJsonValue>>* JsonModules;
					if (HeadObj->TryGetArrayField(TEXT("Modules"), JsonModules))
					{
						for (const auto& ModValue : *JsonModules)
						{
							const TSharedPtr<FJsonObject> ModObj{ModValue.IsValid() ? ModValue->AsObject() : nullptr};
							FTLModule Module;
							if (!ModObj.IsValid())
							{
								UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("Invalid Module object in JSON."));
								continue;
							}
							if (ModObj->HasTypedField<EJson::Object>(TEXT("Transform")))
							{
								Module.Transform = ParseTransform(ModObj->GetObjectField(TEXT("Transform")));
							}
							if (ModObj->HasTypedField<EJson::Object>(TEXT("Offset")))
							{
								Module.Offset = ParseTransform(ModObj->GetObjectField(TEXT("Offset")));
							}
							if (ModObj->HasTypedField<EJson::Boolean>(TEXT("bHasVisor")))
							{
								Module.bHasVisor = ModObj->GetBoolField(TEXT("bHasVisor"));
							}
							if (ModObj->HasTypedField<EJson::String>(TEXT("ModuleMesh")))
							{
								const FString MeshName{ModObj->GetStringField(TEXT("ModuleMesh"))};
								bool MeshFound{false};
								for (UStaticMesh* Mesh : FTLMeshFactory::GetAllMeshesForModule(Head, Module))
								{
									if (IsValid(Mesh) && Mesh->GetName() == MeshName)
									{
										Module.ModuleMesh = Mesh;
										MeshFound = true;
										break;
									}
								}
								if (!MeshFound)
								{
									UE_LOG(LogCarlaDigitalTwinsTool, Warning, TEXT("Module mesh '%s' not found, using default."),
										*MeshName);
									Module.ModuleMesh = FTLMeshFactory::GetMeshForModule(Head, Module);
								}
							}
							else
							{
								UE_LOG(LogCarlaDigitalTwinsTool, Warning, TEXT("Module Mesh Name not found, using default."));
								TArray<UStaticMesh*> Candidates = FTLMeshFactory::GetAllMeshesForModule(Head, Module);
								Module.ModuleMesh = Candidates.IsEmpty() ? nullptr : Candidates.Last();
							}
							const TArray<TSharedPtr<FJsonValue>>* JsonLights;
							if (ModObj->TryGetArrayField(TEXT("Lights"), JsonLights))
							{
								for (const auto& LightValue : *JsonLights)
								{
									const TSharedPtr<FJsonObject> LightObj{LightValue.IsValid() ? LightValue->AsObject() : nullptr};
									if (!LightObj.IsValid())
									{
										continue;
									}
									FTLModuleLight Light;
									FString LightName;
									if (LightObj->TryGetStringField(TEXT("LightType"), LightName))
									{
										Light.LightType = GetEnumValueFromString<ETLLightType>(LightName);
									}
									const FIntPoint AtlasUV{FTLMeshFactory::GetAtlasCoordsForLightType(Light.LightType)};
									Light.U = AtlasUV.X;
									Light.V = AtlasUV.Y;
									Light.EmissiveIntensity = static_cast<float>(
										GetJsonNumberOrDefault(LightObj, TEXT("EmissiveIntensity"), Light.EmissiveIntensity));
									const TSharedPtr<FJsonObject>* ColorObj = nullptr;
									if (LightObj->TryGetObjectField(TEXT("EmissiveColor"), ColorObj) && ColorObj && ColorObj->IsValid())
									{
										Light.EmissiveColor.R = static_cast<float>(GetJsonNumberOrDefault(*ColorObj, TEXT("R"), 0.0));
										Light.EmissiveColor.G = static_cast<float>(GetJsonNumberOrDefault(*ColorObj, TEXT("G"), 0.0));
										Light.EmissiveColor.B = static_cast<float>(GetJsonNumberOrDefault(*ColorObj, TEXT("B"), 0.0));
										Light.EmissiveColor.A = static_cast<float>(GetJsonNumberOrDefault(*ColorObj, TEXT("A"), 1.0));
									}
									Module.Lights.Add(MoveTemp(Light));
								}
							}
							Head.Modules.Add(MoveTemp(Module));
						}
					}
					Pole.Heads.Add(MoveTemp(Head));
				}
			}
			Poles.Add(MoveTemp(Pole));
		}
	}

	// Read new traffic light properties
	FString GroupID;
	if (Root->TryGetStringField(TEXT("TrafficLightGroupID"), GroupID))
	{
		TrafficLightGroupID = GroupID;
	}

	FString JsonSignalID;
	if (Root->TryGetStringField(TEXT("SignalID"), JsonSignalID))
	{
		SignalID = JsonSignalID;
	}

	int32 JsonJunctionID;
	if (Root->TryGetNumberField(TEXT("JunctionID"), JsonJunctionID))
	{
		JunctionID = JsonJunctionID;
	}

	// Read timing information
	double JsonRedDuration;
	if (Root->TryGetNumberField(TEXT("RedDuration"), JsonRedDuration))
	{
		RedDuration = static_cast<float>(JsonRedDuration);
	}

	double JsonGreenDuration;
	if (Root->TryGetNumberField(TEXT("GreenDuration"), JsonGreenDuration))
	{
		GreenDuration = static_cast<float>(JsonGreenDuration);
	}

	double JsonAmberDuration;
	if (Root->TryGetNumberField(TEXT("AmberDuration"), JsonAmberDuration))
	{
		AmberDuration = static_cast<float>(JsonAmberDuration);
	}

	double JsonAmberBlinkInterval;
	if (Root->TryGetNumberField(TEXT("AmberBlinkInterval"), JsonAmberBlinkInterval))
	{
		AmberBlinkInterval = static_cast<float>(JsonAmberBlinkInterval);
	}

	if (!HasAnyFlags(RF_Transient) && GetWorld())
	{
		Build();
	}
}

void ATrafficLightActor::PopulateDefault()
{
	Poles.Empty();
	const FString BaseMeshName{TEXT("SM_TrafficLight_Base")};
	const FString ExtensibleMeshName{TEXT("SM_TrafficLight_Pole04")};
	FTLPole Pole;
	Pole.Transform = FTransform::Identity;
	Pole.Offset = FTransform::Identity;
	Pole.Style = ETLStyle::NorthAmerican;
	Pole.Orientation = ETLOrientation::Vertical;
	Pole.PoleHeight = 260.0f;
	Pole.BasePoleMesh = FTLMeshFactory::GetBaseMeshByName(BaseMeshName);
	Pole.ExtensiblePoleMesh = FTLMeshFactory::GetExtensibleMeshByName(ExtensibleMeshName);

	FTLHead Head;
	Head.Transform = FTransform(FRotator(), FVector(0, 8, 250), FVector(1.25f, 1.25f, 1.25f));
	Head.Offset = FTransform::Identity;
	Head.Style = ETLStyle::NorthAmerican;
	Head.Orientation = ETLOrientation::Vertical;
	Head.bHasBackplate = false;

	auto PickLastModuleMesh = [](const FTLHead& InHead, const FTLModule& InMod) -> UStaticMesh*
	{
		TArray<UStaticMesh*> Candidates = FTLMeshFactory::GetAllMeshesForModule(InHead, InMod);
		return Candidates.IsEmpty() ? nullptr : Candidates.Last();
	};

	FTLModule ModuleGreen;
	ModuleGreen.bHasVisor = true;
	ModuleGreen.ModuleMesh = PickLastModuleMesh(Head, ModuleGreen);
	FTLModuleLight GreenLight;
	GreenLight.LightType = ETLLightType::SolidColorGreen;
	const FIntPoint GreenUV = FTLMeshFactory::GetAtlasCoordsForLightType(GreenLight.LightType);
	GreenLight.U = GreenUV.X;
	GreenLight.V = GreenUV.Y;
	GreenLight.EmissiveColor = FLinearColor{0.100902f, 1.0f, 0.297537f, 1.0f};
	GreenLight.EmissiveIntensity = 50000.0f;
	ModuleGreen.Lights.Add(GreenLight);
	Head.Modules.Add(ModuleGreen);

	FTLModule ModuleAmber;
	ModuleAmber.bHasVisor = true;
	ModuleAmber.ModuleMesh = PickLastModuleMesh(Head, ModuleAmber);
	FTLModuleLight AmberLight;
	AmberLight.LightType = ETLLightType::SolidColorAmber;
	const FIntPoint AmberUV = FTLMeshFactory::GetAtlasCoordsForLightType(AmberLight.LightType);
	AmberLight.U = AmberUV.X;
	AmberLight.V = AmberUV.Y;
	AmberLight.EmissiveColor = FLinearColor{0.930111f, 0.3564f, 0.014444f, 1.0f};
	AmberLight.EmissiveIntensity = 50000.0f;
	ModuleAmber.Lights.Add(AmberLight);
	Head.Modules.Add(ModuleAmber);

	FTLModule ModuleRed;
	ModuleRed.bHasVisor = true;
	ModuleRed.ModuleMesh = PickLastModuleMesh(Head, ModuleRed);
	FTLModuleLight RedLight;
	RedLight.LightType = ETLLightType::SolidColorRed;
	const FIntPoint RedUV = FTLMeshFactory::GetAtlasCoordsForLightType(RedLight.LightType);
	RedLight.U = RedUV.X;
	RedLight.V = RedUV.Y;
	RedLight.EmissiveColor = FLinearColor{1.0f, 0.052026f, 0.061974f, 1.0f};
	RedLight.EmissiveIntensity = 50000.0f;
	ModuleRed.Lights.Add(RedLight);

	Head.Modules.Add(ModuleRed);
	Pole.Heads.Add(Head);
	Poles.Add(Pole);
}

FString ATrafficLightActor::ExportToJSON(bool bUseTransform) const
{
	TSharedPtr<FJsonObject> Root{MakeShared<FJsonObject>()};
	if (bUseTransform)
	{
		{
			FVector Loc{GetActorLocation()};
			TSharedPtr<FJsonObject> JLoc = MakeShared<FJsonObject>();
			JLoc->SetNumberField(TEXT("X"), Loc.X);
			JLoc->SetNumberField(TEXT("Y"), Loc.Y);
			JLoc->SetNumberField(TEXT("Z"), Loc.Z);
			Root->SetObjectField(TEXT("WorldPosition"), JLoc);
		}
		{
			FRotator Rot{GetActorRotation()};
			TSharedPtr<FJsonObject> JRot = MakeShared<FJsonObject>();
			JRot->SetNumberField(TEXT("X"), Rot.Pitch);
			JRot->SetNumberField(TEXT("Y"), Rot.Yaw);
			JRot->SetNumberField(TEXT("Z"), Rot.Roll);
			Root->SetObjectField(TEXT("WorldRotation"), JRot);
		}
		{
			FVector Scl{GetActorScale3D()};
			TSharedPtr<FJsonObject> JScale = MakeShared<FJsonObject>();
			JScale->SetNumberField(TEXT("X"), Scl.X);
			JScale->SetNumberField(TEXT("Y"), Scl.Y);
			JScale->SetNumberField(TEXT("Z"), Scl.Z);
			Root->SetObjectField(TEXT("WorldScale"), JScale);
		}
	}
	TArray<TSharedPtr<FJsonValue>> JsonPoles;
	for (const FTLPole& Pole : Poles)
	{
		TSharedPtr<FJsonObject> JP{MakeShared<FJsonObject>()};

		if (Pole.BasePoleMesh)
		{
			JP->SetStringField(TEXT("BaseMesh"), Pole.BasePoleMesh->GetName());
		}
		if (Pole.ExtensiblePoleMesh)
		{
			JP->SetStringField(TEXT("ExtensibleMesh"), Pole.ExtensiblePoleMesh->GetName());
		}
		if (Pole.CapPoleMesh)
		{
			JP->SetStringField(TEXT("CapMesh"), Pole.CapPoleMesh->GetName());
		}

		JP->SetObjectField(TEXT("Transform"), TransformToJson(Pole.Transform));
		JP->SetObjectField(TEXT("Offset"), TransformToJson(Pole.Offset));
		JP->SetStringField(TEXT("Style"), EnumToString<ETLStyle>(Pole.Style));
		JP->SetStringField(TEXT("Orientation"), EnumToString<ETLOrientation>(Pole.Orientation));
		JP->SetNumberField(TEXT("PoleHeight"), Pole.PoleHeight);

		TArray<TSharedPtr<FJsonValue>> JsonHeads;
		for (const FTLHead& Head : Pole.Heads)
		{
			TSharedPtr<FJsonObject> JH = MakeShared<FJsonObject>();
			JH->SetObjectField(TEXT("Transform"), TransformToJson(Head.Transform));
			JH->SetObjectField(TEXT("Offset"), TransformToJson(Head.Offset));
			JH->SetStringField(TEXT("Style"), EnumToString<ETLStyle>(Head.Style));
			JH->SetStringField(TEXT("Attachment"), EnumToString<ETLHeadAttachment>(Head.Attachment));
			JH->SetStringField(TEXT("Orientation"), EnumToString<ETLOrientation>(Head.Orientation));
			JH->SetBoolField(TEXT("bHasBackplate"), Head.bHasBackplate);
			if (!Head.SignalID.IsEmpty())
			{
				JH->SetStringField(TEXT("SignalID"), Head.SignalID);
			}

			TArray<TSharedPtr<FJsonValue>> JsonModules;
			for (const FTLModule& Module : Head.Modules)
			{
				TSharedPtr<FJsonObject> JM{MakeShared<FJsonObject>()};
				if (Module.ModuleMesh)
				{
					JM->SetStringField(TEXT("ModuleMesh"), Module.ModuleMesh->GetName());
				}
				JM->SetObjectField(TEXT("Transform"), TransformToJson(Module.Transform));
				JM->SetObjectField(TEXT("Offset"), TransformToJson(Module.Offset));
				JM->SetBoolField(TEXT("bHasVisor"), Module.bHasVisor);

				TArray<TSharedPtr<FJsonValue>> JsonLights;
				for (const FTLModuleLight& L : Module.Lights)
				{
					TSharedPtr<FJsonObject> JL{MakeShared<FJsonObject>()};
					JL->SetStringField(TEXT("LightType"), EnumToString<ETLLightType>(L.LightType));
					JL->SetNumberField(TEXT("EmissiveIntensity"), L.EmissiveIntensity);
					TSharedPtr<FJsonObject> JC = MakeShared<FJsonObject>();
					JC->SetNumberField(TEXT("R"), L.EmissiveColor.R);
					JC->SetNumberField(TEXT("G"), L.EmissiveColor.G);
					JC->SetNumberField(TEXT("B"), L.EmissiveColor.B);
					JC->SetNumberField(TEXT("A"), L.EmissiveColor.A);
					JL->SetObjectField(TEXT("EmissiveColor"), JC);

					JsonLights.Add(MakeShared<FJsonValueObject>(JL));
				}
				JM->SetArrayField(TEXT("Lights"), JsonLights);

				JsonModules.Add(MakeShared<FJsonValueObject>(JM));
			}
			JH->SetArrayField(TEXT("Modules"), JsonModules);

			JsonHeads.Add(MakeShared<FJsonValueObject>(JH));
		}
		JP->SetArrayField(TEXT("Heads"), JsonHeads);

		JsonPoles.Add(MakeShared<FJsonValueObject>(JP));
	}

	Root->SetArrayField(TEXT("Poles"), JsonPoles);

	// Add new traffic light properties
	Root->SetStringField(TEXT("TrafficLightGroupID"), TrafficLightGroupID);
	Root->SetNumberField(TEXT("JunctionID"), JunctionID);
	Root->SetStringField(TEXT("SignalID"), SignalID);

	// Add timing information
	Root->SetNumberField(TEXT("RedDuration"), RedDuration);
	Root->SetNumberField(TEXT("GreenDuration"), GreenDuration);
	Root->SetNumberField(TEXT("AmberDuration"), AmberDuration);
	Root->SetNumberField(TEXT("AmberBlinkInterval"), AmberBlinkInterval);

	FString Output;
	TSharedRef<TJsonWriter<>> Writer{TJsonWriterFactory<>::Create(&Output)};
	FJsonSerializer::Serialize(Root.ToSharedRef(), Writer);
	return Output;
}

FString ATrafficLightActor::ExportLogicToJSON() const
{
	return ExportLogicToJSON(GetName());
}

FString ATrafficLightActor::ExportLogicToJSON(const FString& ActorName) const
{
	TSharedPtr<FJsonObject> Root = MakeShareable(new FJsonObject);

	// Basic actor information
	Root->SetStringField(TEXT("ActorName"), ActorName);
	Root->SetStringField(TEXT("SignalID"), SignalID);
	Root->SetNumberField(TEXT("JunctionID"), JunctionID);
	Root->SetStringField(TEXT("TrafficLightGroupID"), TrafficLightGroupID);

	// Timing information
	TSharedPtr<FJsonObject> Timing = MakeShareable(new FJsonObject);
	Timing->SetNumberField(TEXT("RedDuration"), RedDuration);
	Timing->SetNumberField(TEXT("GreenDuration"), GreenDuration);
	Timing->SetNumberField(TEXT("AmberDuration"), AmberDuration);
	Timing->SetNumberField(TEXT("AmberBlinkInterval"), AmberBlinkInterval);
	Root->SetObjectField(TEXT("Timing"), Timing);

	// Collect all unique lane IDs from all modules (since all modules control the same lanes)
	TSet<int32> UniqueLaneIds;
	for (const FTLPole& Pole : Poles)
	{
		for (const FTLHead& Head : Pole.Heads)
		{
			for (const FTLModule& Module : Head.Modules)
			{
				for (int32 LaneId : Module.LaneIds)
				{
					UniqueLaneIds.Add(LaneId);
				}
			}
		}
	}

	// Create single module entry with all unique lane IDs
	TArray<TSharedPtr<FJsonValue>> ModulesArray;
	if (UniqueLaneIds.Num() > 0)
	{
		TSharedPtr<FJsonObject> ModuleObj = MakeShareable(new FJsonObject);

		// Convert unique lane IDs to JSON array
		TArray<TSharedPtr<FJsonValue>> LaneIdsArray;
		for (int32 LaneId : UniqueLaneIds)
		{
			LaneIdsArray.Add(MakeShareable(new FJsonValueNumber(LaneId)));
		}
		ModuleObj->SetArrayField(TEXT("LaneIds"), LaneIdsArray);

		ModulesArray.Add(MakeShareable(new FJsonValueObject(ModuleObj)));
	}
	Root->SetArrayField(TEXT("Modules"), ModulesArray);

	TArray<TSharedPtr<FJsonValue>> HeadsArray;
	for (int32 PoleIndex = 0; PoleIndex < Poles.Num(); ++PoleIndex)
	{
		const FTLPole& Pole = Poles[PoleIndex];
		for (int32 HeadIndex = 0; HeadIndex < Pole.Heads.Num(); ++HeadIndex)
		{
			const FTLHead& Head = Pole.Heads[HeadIndex];
			TSharedPtr<FJsonObject> HeadObj = MakeShareable(new FJsonObject);
			HeadObj->SetStringField(TEXT("ComponentPrefix"),
				FString::Printf(TEXT("Pole_%02d_Head_%02d"), PoleIndex, HeadIndex));
			HeadObj->SetStringField(TEXT("SignalID"),
				Head.SignalID.IsEmpty() ? SignalID : Head.SignalID);

			TSet<int32> HeadLaneIds;
			TArray<TSharedPtr<FJsonValue>> LightTypes;
			for (const FTLModule& Module : Head.Modules)
			{
				for (int32 LaneId : Module.LaneIds)
				{
					HeadLaneIds.Add(LaneId);
				}
				for (const FTLModuleLight& L : Module.Lights)
				{
					LightTypes.Add(MakeShareable(
						new FJsonValueString(EnumToString<ETLLightType>(L.LightType))));
				}
			}
			TArray<TSharedPtr<FJsonValue>> HeadLanes;
			for (int32 LaneId : HeadLaneIds)
			{
				HeadLanes.Add(MakeShareable(new FJsonValueNumber(LaneId)));
			}
			HeadObj->SetArrayField(TEXT("LaneIds"), HeadLanes);
			HeadObj->SetArrayField(TEXT("LightTypes"), LightTypes);
			HeadsArray.Add(MakeShareable(new FJsonValueObject(HeadObj)));
		}
	}
	Root->SetArrayField(TEXT("Heads"), HeadsArray);

	FString Output;
	TSharedRef<TJsonWriter<>> Writer{TJsonWriterFactory<>::Create(&Output)};
	FJsonSerializer::Serialize(Root.ToSharedRef(), Writer);
	return Output;
}

USceneComponent* ATrafficLightActor::AddRootPole(USceneComponent* Parent, FTLPole& Pole)
{
	USceneComponent* PoleRoot{UMapGenFunctionLibrary::AddSceneComponentToActor(this)};
	if (!PoleRoot)
	{
		return nullptr;
	}

	PoleRoot->AttachToComponent(Parent, FAttachmentTransformRules::KeepRelativeTransform);
	PoleRoot->SetRelativeTransform(Pole.Transform);

	return PoleRoot;
}

USceneComponent* ATrafficLightActor::AddHead(USceneComponent* Parent, FTLPole& Pole, FTLHead& Head)
{
	USceneComponent* HeadRoot{UMapGenFunctionLibrary::AddSceneComponentToActor(this)};
	if (!HeadRoot)
	{
		return nullptr;
	}

	HeadRoot->AttachToComponent(Parent, FAttachmentTransformRules::KeepRelativeTransform);
	HeadRoot->SetRelativeTransform(Head.Transform);

	return HeadRoot;
}

UStaticMeshComponent* ATrafficLightActor::AddModule(USceneComponent* Parent, FTLPole& Pole, FTLHead& Head, FTLModule& ModuleData)
{
	const FTransform ModuleTransform{ModuleData.Transform * ModuleData.Offset};
	const FString MeshName = IsValid(ModuleData.ModuleMesh) ? ModuleData.ModuleMesh->GetName() : TEXT("None");

	UStaticMeshComponent* Comp{UMapGenFunctionLibrary::AddStaticMeshComponentToActor(this)};
	if (!IsValid(Comp))
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Warning, TEXT("Failed to create StaticMeshComponent for module %s"), *MeshName);
		return nullptr;
	}
	Comp->SetStaticMesh(ModuleData.ModuleMesh);
	Comp->AttachToComponent(Parent, FAttachmentTransformRules::KeepRelativeTransform);
	Comp->SetRelativeTransform(ModuleTransform);

	int32 LightIndex{0};
	for (FTLModuleLight& Light : ModuleData.Lights)
	{
		const FName SlotName{*FString::Printf(TEXT("led_%d"), LightIndex)};
		const int32 MaterialSlotIndex{Comp->GetMaterialIndex(SlotName)};
		if (MaterialSlotIndex != INDEX_NONE)
		{
			UMaterialInstanceDynamic* MID{FMaterialFactory::CreateLightMaterialInstanceDynamic(Comp, SlotName)};
			if (IsValid(MID))
			{
				MID->SetScalarParameterValue(TEXT("Emissive Intensity"), Light.EmissiveIntensity);
				MID->SetVectorParameterValue(TEXT("Emissive Color"), Light.EmissiveColor);
				MID->SetScalarParameterValue(TEXT("Offset U"), static_cast<float>(Light.U));
				MID->SetScalarParameterValue(TEXT("Offset V"), static_cast<float>(Light.V));
				Comp->SetMaterial(MaterialSlotIndex, MID);
				Light.LightMID = MID;
				DemoLights.Add(&Light);
			}
			else
			{
				UE_LOG(LogCarlaDigitalTwinsTool, Warning, TEXT("Failed to create MID for slot %s"), *SlotName.ToString());
			}
		}
		else
		{
			UE_LOG(LogCarlaDigitalTwinsTool, Warning, TEXT("Material slot %s not found in module %s"), *SlotName.ToString(),
				*MeshName);
		}
		++LightIndex;
	}

	Comp->Modify();
	ModuleData.ModuleMeshComponent = Comp;
	return Comp;
}

UStaticMeshComponent* ATrafficLightActor::AddPoleBase(USceneComponent* Parent, FTLPole& Pole)
{
	if (!IsValid(Pole.BasePoleMesh))
	{
		return nullptr;
	}

	UStaticMeshComponent* Comp{UMapGenFunctionLibrary::AddStaticMeshComponentToActor(this)};
	if (!Comp)
	{
		return nullptr;
	}

	Comp->SetStaticMesh(Pole.BasePoleMesh);
	Comp->AttachToComponent(Parent, FAttachmentTransformRules::KeepRelativeTransform);
	Comp->SetRelativeTransform(Pole.Offset);
	Comp->Modify();

	Pole.BasePoleMeshComponent = Comp;
	return Comp;
}

UStaticMeshComponent* ATrafficLightActor::AddPoleExtensible(USceneComponent* Parent, FTLPole& Pole)
{
	if (!IsValid(Pole.ExtensiblePoleMesh))
	{
		return nullptr;
	}

	UStaticMeshComponent* BaseComp{Pole.BasePoleMeshComponent};
	UStaticMeshComponent* ExtComp{UMapGenFunctionLibrary::AddStaticMeshComponentToActor(this)};
	if (!ExtComp)
	{
		return nullptr;
	}
	ExtComp->SetStaticMesh(Pole.ExtensiblePoleMesh);
	static const FName SocketName("extensible");
	if (IsValid(BaseComp))
	{
		ExtComp->AttachToComponent(BaseComp, FAttachmentTransformRules::SnapToTargetIncludingScale, SocketName);
	}
	else
	{
		ExtComp->AttachToComponent(Parent, FAttachmentTransformRules::KeepRelativeTransform);
	}
	double BaseLength{0.0};
	if (IsValid(BaseComp) && IsValid(BaseComp->GetStaticMesh()))
	{
		const FBoxSphereBounds Bounds{BaseComp->GetStaticMesh()->GetBounds()};
		BaseLength = (Pole.Orientation == ETLOrientation::Horizontal) ? Bounds.BoxExtent.X * 2.0 : Bounds.BoxExtent.Z * 2.0;
	}

	const FBoxSphereBounds ExtBounds{Pole.ExtensiblePoleMesh->GetBounds()};
	const double ExtLength{
		(Pole.Orientation == ETLOrientation::Horizontal) ? ExtBounds.BoxExtent.X * 2.0 : ExtBounds.BoxExtent.Z * 2.0};
	const double DesiredExtHeight{FMath::Max(Pole.PoleHeight - BaseLength, 0.0)};
	if (ExtLength > KINDA_SMALL_NUMBER)
	{
		const double ScaleRatio{DesiredExtHeight / ExtLength};
		FVector Scale3D{ExtComp->GetRelativeScale3D()};
		if (Pole.Orientation == ETLOrientation::Horizontal)
		{
			Scale3D.X = ScaleRatio;
		}
		else
		{
			Scale3D.Z = ScaleRatio;
		}
		ExtComp->SetRelativeScale3D(Scale3D);
	}
	else
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Warning, TEXT("Extensible mesh has zero length, cannot scale properly."));
	}

	ExtComp->Modify();
	Pole.ExtensiblePoleMeshComponent = ExtComp;
	return ExtComp;
}

UStaticMeshComponent* ATrafficLightActor::AddPoleCap(USceneComponent* Parent, FTLPole& Pole)
{
	if (!IsValid(Pole.CapPoleMesh))
	{
		return nullptr;
	}
	UStaticMeshComponent* ExtensibleComp{Pole.ExtensiblePoleMeshComponent};
	if (!IsValid(ExtensibleComp))
	{
		return nullptr;
	}

	static const FName CapSocketName(TEXT("cap"));
	if (!ExtensibleComp->DoesSocketExist(CapSocketName))
	{
		UE_LOG(
			LogCarlaDigitalTwinsTool, Warning, TEXT("Socket '%s' not found in extensible component."), *CapSocketName.ToString());
		return nullptr;
	}

	UStaticMeshComponent* Comp{UMapGenFunctionLibrary::AddStaticMeshComponentToActor(this)};
	if (!Comp)
	{
		return nullptr;
	}

	Comp->SetStaticMesh(Pole.CapPoleMesh);
	Comp->AttachToComponent(ExtensibleComp, FAttachmentTransformRules::SnapToTargetNotIncludingScale, CapSocketName);
	Comp->Modify();

	Pole.CapPoleMeshComponent = Comp;
	return Comp;
}

void ATrafficLightActor::AddBackplate(USceneComponent* HeadRoot, FTLPole& Pole, FTLHead& Head)
{
	if (!IsValid(HeadRoot))
	{
		return;
	}

	FBox LocalBounds{ForceInit};
	for (const FTLModule& Module : Head.Modules)
	{
		if (!IsValid(Module.ModuleMeshComponent) || !IsValid(Module.ModuleMeshComponent->GetStaticMesh()))
		{
			continue;
		}

		const FBox MeshBB{Module.ModuleMeshComponent->GetStaticMesh()->GetBoundingBox()};
		LocalBounds += MeshBB.TransformBy(Module.ModuleMeshComponent->GetRelativeTransform());
	}
	if (!LocalBounds.IsValid)
	{
		return;
	}

	UStaticMesh* VertMesh = FTLMeshFactory::GetBackplateVerticalMesh(Head);
	UStaticMesh* HorzMesh = FTLMeshFactory::GetBackplateHorizontalMesh(Head);
	UStaticMesh* CornerMesh = FTLMeshFactory::GetBackplateCornerMesh(Head);
	UStaticMesh* MiddleMesh = FTLMeshFactory::GetBackplateMiddleMesh(Head);
	if (!IsValid(VertMesh) || !IsValid(HorzMesh))
	{
		return;
	}

	const double VertBaseLen = VertMesh->GetBoundingBox().GetExtent().Z * 2.0;
	const double HorzBaseLen = HorzMesh->GetBoundingBox().GetExtent().X * 2.0;
	if (VertBaseLen <= KINDA_SMALL_NUMBER || HorzBaseLen <= KINDA_SMALL_NUMBER)
	{
		return;
	}

	const FVector LMin{LocalBounds.Min};
	const FVector LMax{LocalBounds.Max};

	const double LocalHeight{LMax.Z - LMin.Z};
	const double ZScaleVert{LocalHeight / VertBaseLen};

	const double LocalWidth{LMax.X - LMin.X};
	const double XScaleHorz{LocalWidth / HorzBaseLen};

	auto SpawnLocalPiece = [&](UStaticMesh* Mesh, const FVector& Loc, const FRotator& Rot, const FVector& Scale)
	{
		if (!IsValid(Mesh))
		{
			return;
		}
		UStaticMeshComponent* Comp{UMapGenFunctionLibrary::AddStaticMeshComponentToActor(this)};
		if (!IsValid(Comp))
		{
			return;
		}

		Comp->SetStaticMesh(Mesh);
		Comp->AttachToComponent(HeadRoot, FAttachmentTransformRules::KeepRelativeTransform);

		const double HalfDepth{Mesh->GetBoundingBox().GetExtent().Y};
		const FVector AdjustLoc{Loc.X, Loc.Y - HalfDepth, Loc.Z};

		Comp->SetRelativeLocation(AdjustLoc);
		Comp->SetRelativeRotation(Rot);
		Comp->SetRelativeScale3D(Scale);
		Comp->Modify();
	};

	struct FPlacement
	{
		FVector Loc;
		FRotator Rot;
	};
	const TArray<FPlacement> Corners{{{LMin.X, LMin.Y, LMax.Z}, {0.0, 0.0, 0.0}}, {{LMin.X, LMin.Y, LMin.Z}, {90.0, 0.0, 0.0}},
		{{LMax.X, LMin.Y, LMin.Z}, {180.0, 0.0, 0.0}}, {{LMax.X, LMin.Y, LMax.Z}, {270.0, 0.0, 0.0}}};
	for (const FPlacement& P : Corners)
	{
		SpawnLocalPiece(CornerMesh, P.Loc, P.Rot, FVector{1.0, 1.0, 1.0});
	}

	const TArray<FPlacement> Verticals{
		{{LMin.X, LMin.Y, LMin.Z}, {0.0, 0.0, 0.0}}, {{LMax.X, LMin.Y, LMin.Z + LocalHeight}, {180.0, 0.0, 0.0}}};
	for (const FPlacement& P : Verticals)
	{
		SpawnLocalPiece(VertMesh, P.Loc, P.Rot, FVector{1.0, 1.0, ZScaleVert});
	}

	const TArray<FPlacement> Horizontals{
		{{LMin.X + LocalWidth, LMin.Y, LMax.Z}, {0.0, 0.0, 0.0}}, {{LMin.X, LMin.Y, LMin.Z}, {180.0, 0.0, 0.0}}};
	for (const FPlacement& P : Horizontals)
	{
		SpawnLocalPiece(HorzMesh, P.Loc, P.Rot, FVector{XScaleHorz, 1.0, 1.0});
	}

	const FVector CenterLoc{LMin.X + (LMax.X - LMin.X), LMin.Y, LMin.Z};
	SpawnLocalPiece(MiddleMesh, CenterLoc, FRotator{0.0, 0.0, 0.0}, FVector{XScaleHorz, 1.0, ZScaleVert});
}

void ATrafficLightActor::RebuildModuleChain(FTLHead& Head)
{
	if (Head.Modules.IsEmpty())
	{
		return;
	}

	static const FName EndSocketName{FName("Socket2")};
	static const FName BeginSocketName{FName("Socket1")};

	{
		FTLModule& Module{Head.Modules[0]};
		Module.Transform = FTransform::Identity;
		if (Module.ModuleMeshComponent)
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
			UE_LOG(LogCarlaDigitalTwinsTool, Warning, TEXT("Current module mesh/component is invalid at module %d"), i);
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

void ATrafficLightActor::Clear()
{
#if WITH_EDITOR
	if (USceneComponent * Root{GetRootComponent()})
	{
		TArray<USceneComponent*> AllComponents;
		Root->GetChildrenComponents(true, AllComponents);
		for (USceneComponent* Comp : AllComponents)
		{
			if (IsValid(Comp))
			{
				Comp->DestroyComponent(false);
			}
		}
	}

	{
		TArray<UActorComponent*> AllComponents;
		GetComponents(AllComponents);
		for (UActorComponent* Comp : AllComponents)
		{
			if (!IsValid(Comp))
				continue;

			if (USceneComponent * SC{Cast<USceneComponent>(Comp)})
			{
				if (SC == GetRootComponent())
				{
					continue;
				}
			}

			if (Comp->CreationMethod == EComponentCreationMethod::UserConstructionScript)
			{
				Comp->DestroyComponent();
			}
		}
	}

	ModuleMeshComponents.Reset();
	DemoLights.Reset();
#endif
}

USceneComponent* ATrafficLightActor::EnsurePolesRoot()
{
	USceneComponent* Root{GetRootComponent()};
	if (!IsValid(Root))
	{
		Root = UMapGenFunctionLibrary::AddSceneComponentToActor(this);
		SetRootComponent(Root);
	}
	if (!IsValid(Root))
	{
		return nullptr;
	}
	const FString CurrentName{Root->GetName()};
	if (!CurrentName.StartsWith(TEXT("Poles")))
	{
		const FString Desired{MakeUniqueNamed(TEXT("Poles"))};
		Root->Rename(*Desired, this, REN_DontCreateRedirectors);
	}
	return Root;
}

void ATrafficLightActor::ResetAllLights()
{
	for (FTLModuleLight* Light : DemoLights)
	{
		if (Light && IsValid(Light->LightMID))
		{
			Light->LightMID->SetScalarParameterValue(TEXT("Emissive Intensity"), 0.0f);
		}
	}
}

void ATrafficLightActor::PlayDemoSequence()
{
	if (!GetWorld())
	{
		return;
	}
	DemoLights.Empty();
	for (FTLPole& Pole : Poles)
	{
		for (FTLHead& Head : Pole.Heads)
		{
			for (FTLModule& Module : Head.Modules)
			{
				for (FTLModuleLight& Light : Module.Lights)
				{
					if (IsValid(Light.LightMID))
					{
						UE_LOG(LogCarlaDigitalTwinsTool, Log, TEXT("Adding light %s to demo sequence."),
							*UEnum::GetValueAsString(Light.LightType));
						DemoLights.Add(&Light);
					}
				}
			}
		}
	}
	bDemoPlaying = true;
	CurrentPhase = EDemoPhase::Red;
	GetWorldTimerManager().ClearTimer(PhaseTimerHandle);
	GetWorldTimerManager().ClearTimer(AmberBlinkTimerHandle);
	AdvanceDemoPhase();
}

void ATrafficLightActor::StopDemoSequence()
{
	UE_LOG(LogCarlaDigitalTwinsTool, Log, TEXT("StopDemoSequence called, stopping traffic light demo sequence."));
	bDemoPlaying = false;
	if (GetWorld())
	{
		GetWorldTimerManager().ClearTimer(PhaseTimerHandle);
		GetWorldTimerManager().ClearTimer(AmberBlinkTimerHandle);
	}
}

void ATrafficLightActor::AdvanceDemoPhase()
{
	if (!bDemoPlaying || !GetWorld())
	{
		return;
	}
	ResetAllLights();
	auto IsPhase{[&](FTLModuleLight* L, const FString& PhaseName)
		{
			const FString Full{UEnum::GetValueAsString(L->LightType)};
			return Full.Contains(PhaseName, ESearchCase::IgnoreCase);
		}};

	switch (CurrentPhase)
	{
		case EDemoPhase::Red:
		{
			for (FTLModuleLight* Light : DemoLights)
			{
				if (Light && IsValid(Light->LightMID) && (IsPhase(Light, "Red") || IsPhase(Light, "Stop") || IsPhase(Light, "Horizontal")))
				{
					Light->LightMID->SetScalarParameterValue(TEXT("Emissive Intensity"), Light->EmissiveIntensity);
				}
			}
			CurrentPhase = EDemoPhase::Green;
			GetWorldTimerManager().ClearTimer(PhaseTimerHandle);
			GetWorldTimerManager().SetTimer(
				PhaseTimerHandle, this, &ATrafficLightActor::AdvanceDemoPhase, FMath::Max(GetRedDuration(), 0.05f), false);
			break;
		}
		case EDemoPhase::Green:
		{
			for (FTLModuleLight* Light : DemoLights)
			{
				if (Light && IsValid(Light->LightMID) && (IsPhase(Light, "Green") || IsPhase(Light, "Walk") || IsPhase(Light, "Vertical")))
				{
					Light->LightMID->SetScalarParameterValue("Emissive Intensity", Light->EmissiveIntensity);
				}
			}
			const float MinAmberThreshold{0.25f};
			const float SafeGreenDuration = FMath::Max(GetGreenDuration(), 0.05f);
			if (GetAmberDuration() <= MinAmberThreshold)
			{
				CurrentPhase = EDemoPhase::Red;
				GetWorldTimerManager().ClearTimer(PhaseTimerHandle);
				GetWorldTimerManager().SetTimer(
					PhaseTimerHandle, this, &ATrafficLightActor::AdvanceDemoPhase, SafeGreenDuration, false);
			}
			else
			{
				CurrentPhase = EDemoPhase::Amber;
				bAmberVisible = false;
				GetWorldTimerManager().ClearTimer(PhaseTimerHandle);
				GetWorldTimerManager().SetTimer(
					PhaseTimerHandle, this, &ATrafficLightActor::AdvanceDemoPhase, SafeGreenDuration, false);
			}
			break;
		}
		case EDemoPhase::Amber:
		{
			bAmberVisible = false;
			ToggleAmberBlink();
			GetWorldTimerManager().ClearTimer(AmberBlinkTimerHandle);
			GetWorldTimerManager().SetTimer(
				AmberBlinkTimerHandle, this, &ATrafficLightActor::ToggleAmberBlink, FMath::Max(GetAmberBlinkInterval(), 0.05f), true);
			GetWorldTimerManager().ClearTimer(PhaseTimerHandle);
			GetWorldTimerManager().SetTimer(
				PhaseTimerHandle, this, &ATrafficLightActor::EndAmberPhase, FMath::Max(GetAmberDuration(), 0.05f), false);
			break;
		}
	}
}

void ATrafficLightActor::ToggleAmberBlink()
{
	if (!bDemoPlaying)
	{
		return;
	}
	ResetAllLights();
	auto IsPhase{[&](FTLModuleLight* L, const FString& PhaseName)
		{
			const FString Full{UEnum::GetValueAsString(L->LightType)};
			return Full.Contains(PhaseName, ESearchCase::IgnoreCase);
		}};

	bAmberVisible = !bAmberVisible;
	for (FTLModuleLight* Light : DemoLights)
	{
		if (Light && IsValid(Light->LightMID) && IsPhase(Light, "Amber"))
		{
			Light->LightMID->SetScalarParameterValue("Emissive Intensity", bAmberVisible ? Light->EmissiveIntensity : 0.0f);
		}
	}
}

void ATrafficLightActor::EndAmberPhase()
{
	GetWorldTimerManager().ClearTimer(AmberBlinkTimerHandle);
	CurrentPhase = EDemoPhase::Red;
	AdvanceDemoPhase();
}

float ATrafficLightActor::GetRedDuration() const
{
	return RedDuration;
}

float ATrafficLightActor::GetGreenDuration() const
{
	return GreenDuration;
}

float ATrafficLightActor::GetAmberDuration() const
{
	return AmberDuration;
}

float ATrafficLightActor::GetAmberBlinkInterval() const
{
	return AmberBlinkInterval;
}

