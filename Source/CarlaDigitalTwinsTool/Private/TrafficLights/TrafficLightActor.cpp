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
FTransform ParseTransform(const TSharedPtr<FJsonObject>& Json)
{
	const auto& Loc{Json->GetObjectField(TEXT("Location"))};
	const auto& Rot{Json->GetObjectField(TEXT("Rotation"))};
	const auto& Scale{Json->GetObjectField(TEXT("Scale"))};
	const FVector Location(Loc->GetNumberField(TEXT("X")), Loc->GetNumberField(TEXT("Y")), Loc->GetNumberField(TEXT("Z")));
	const FRotator Rotation(
		Rot->GetNumberField(TEXT("Pitch")), Rot->GetNumberField(TEXT("Yaw")), Rot->GetNumberField(TEXT("Roll")));
	const FVector Scale3D(Scale->GetNumberField(TEXT("X")), Scale->GetNumberField(TEXT("Y")), Scale->GetNumberField(TEXT("Z")));
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

// Convierte un enum a su nombre en string
template <typename TEnum>
FString EnumToString(TEnum Value)
{
	const UEnum* EnumPtr = StaticEnum<TEnum>();
	return EnumPtr ? EnumPtr->GetNameStringByValue(static_cast<int64>(Value)) : FString();
}
}	 // namespace

ATrafficLightActor::ATrafficLightActor()
{
	RootComponent = CreateDefaultSubobject<USceneComponent>(TEXT("Root"));
}

void ATrafficLightActor::OnConstruction(const FTransform& Transform)
{
	Super::OnConstruction(Transform);
#if WITH_EDITOR
	if (HasAnyFlags(RF_Transient))
	{
		Build();
	}
#endif

	TArray<UMaterialInstanceDynamic*> s_DemoLightsInstances{};
}

void ATrafficLightActor::Bake(const FString& MapName)
{
#if WITH_EDITOR
	TArray<UStaticMeshComponent*> MeshesToUpdateWithPluginRef;
	UWorld* World{GetWorld()};
	if (!World || !World->IsEditorWorld())
	{
		UE_LOG(LogCarlaDigitalTwinsTool, Warning, TEXT("Bake: only works in editor world"));
		return;
	}
	const FString FolderName{GetActorLabel()};
	FActorSpawnParameters SpawnParams;
	SpawnParams.SpawnCollisionHandlingOverride = ESpawnActorCollisionHandlingMethod::AlwaysSpawn;
	SpawnParams.ObjectFlags |= RF_Transactional;

	TArray<UStaticMeshComponent*> MeshComps;
	GetComponents<UStaticMeshComponent>(MeshComps);

	for (UStaticMeshComponent* Comp : MeshComps)
	{
		if (!IsValid(Comp) || !Comp->GetStaticMesh())
		{
			continue;
		}

		const FTransform WorldTransform{Comp->GetComponentTransform()};
		AStaticMeshActor* NewActor{
			World->SpawnActor<AStaticMeshActor>(AStaticMeshActor::StaticClass(), WorldTransform, SpawnParams)};

		if (!IsValid(NewActor))
		{
			UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("Bake: failed to spawn mesh for %s"), *Comp->GetName());
			continue;
		}

		UStaticMeshComponent* NewSMC{NewActor->GetStaticMeshComponent()};
		NewSMC->SetStaticMesh(Comp->GetStaticMesh());

		const int32 NumMats{Comp->GetNumMaterials()};
		for (int32 MatIndex{0}; MatIndex < NumMats; ++MatIndex)
		{
			NewSMC->SetMaterial(MatIndex, Comp->GetMaterial(MatIndex));
		}
		MeshesToUpdateWithPluginRef.Add(NewSMC);

		NewActor->SetActorLabel(Comp->GetName());
		NewActor->SetFolderPath(*FolderName);
		NewActor->SetIsTemporarilyHiddenInEditor(false);
	}

	for (UStaticMeshComponent* Comp : MeshComps)
	{
		UStaticMesh* MeshToSet = Cast<UStaticMesh>(UBlueprintUtilFunctions::CopyAssetToPlugin(Comp->GetStaticMesh(), MapName));
		Comp->SetStaticMesh(MeshToSet);
	}

	for (UStaticMeshComponent* Comp : MeshesToUpdateWithPluginRef)
	{
		UStaticMesh* MeshToSet = Cast<UStaticMesh>(UBlueprintUtilFunctions::CopyAssetToPlugin(Comp->GetStaticMesh(), MapName));
		Comp->SetStaticMesh(MeshToSet);
	}

#endif
}

void ATrafficLightActor::Build()
{
	Clear();
	for (FTLPole& Pole : Poles)
	{
		USceneComponent* PoleRoot{AddRootPole(RootComponent, Pole)};
		AddPoleBase(PoleRoot, Pole);
		AddPoleExtensible(PoleRoot, Pole);
		AddPoleCap(PoleRoot, Pole);

		for (FTLHead& Head : Pole.Heads)
		{
			USceneComponent* HeadRoot{AddHead(PoleRoot, Pole, Head)};

			for (FTLModule& Mod : Head.Modules)
			{
				AddModule(HeadRoot, Pole, Head, Mod);
			}
			RebuildModuleChain(Head);
			if (Head.bHasBackplate)
			{
				AddBackplate(HeadRoot, Pole, Head);
			}
		}
	}
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

		const TSharedPtr<FJsonObject>* PosObj;
		if (Root->TryGetObjectField(TEXT("WorldPosition"), PosObj))
		{
			const auto& JO = **PosObj;
			NewTx.SetLocation(FVector{JO.GetNumberField(TEXT("X")), JO.GetNumberField(TEXT("Y")), JO.GetNumberField(TEXT("Z"))});
		}

		const TSharedPtr<FJsonObject>* RotObj;
		if (Root->TryGetObjectField(TEXT("WorldRotation"), RotObj))
		{
			const auto& JO = **RotObj;
			NewTx.SetRotation(
				FQuat(FRotator{JO.GetNumberField(TEXT("X")), JO.GetNumberField(TEXT("Y")), JO.GetNumberField(TEXT("Z"))}));
		}
		const TSharedPtr<FJsonObject>* SclObj;
		if (Root->TryGetObjectField(TEXT("WorldScale"), SclObj))
		{
			const auto& JO = **SclObj;
			NewTx.SetScale3D(FVector{JO.GetNumberField(TEXT("X")), JO.GetNumberField(TEXT("Y")), JO.GetNumberField(TEXT("Z"))});
		}

		SetActorTransform(NewTx);
	}

	Poles.Empty();
	const TArray<TSharedPtr<FJsonValue>>* JsonPoles;
	if (Root->TryGetArrayField(TEXT("Poles"), JsonPoles))
	{
		for (const auto& PoleValue : *JsonPoles)
		{
			const TSharedPtr<FJsonObject> PoleObj{PoleValue->AsObject()};
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
					const TSharedPtr<FJsonObject> HeadObj{HeadValue->AsObject()};
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

					const TArray<TSharedPtr<FJsonValue>>* JsonModules;
					if (HeadObj->TryGetArrayField(TEXT("Modules"), JsonModules))
					{
						for (const auto& ModValue : *JsonModules)
						{
							const TSharedPtr<FJsonObject> ModObj{ModValue->AsObject()};
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
									if (Mesh->GetName() == MeshName)
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
								UE_LOG(LogCarlaDigitalTwinsTool, Log, TEXT("Module Mesh Name not found, using default."));
								Module.ModuleMesh = FTLMeshFactory::GetAllMeshesForModule(Head, Module).Last();
							}
							const TArray<TSharedPtr<FJsonValue>>* JsonLights;
							if (ModObj->TryGetArrayField(TEXT("Lights"), JsonLights))
							{
								for (const auto& LightValue : *JsonLights)
								{
									const TSharedPtr<FJsonObject> LightObj{LightValue->AsObject()};
									FTLModuleLight Light;
									Light.LightType =
										GetEnumValueFromString<ETLLightType>(LightObj->GetStringField(TEXT("LightType")));
									const FVector2D AtlasUV{FTLMeshFactory::GetAtlasCoordsForLightType(Light.LightType)};
									Light.U = AtlasUV.X;
									Light.V = AtlasUV.Y;
									Light.EmissiveIntensity = LightObj->GetNumberField(TEXT("EmissiveIntensity"));
									const auto& ColorObj{LightObj->GetObjectField(TEXT("EmissiveColor"))};
									Light.EmissiveColor.R = ColorObj->GetNumberField(TEXT("R"));
									Light.EmissiveColor.G = ColorObj->GetNumberField(TEXT("G"));
									Light.EmissiveColor.B = ColorObj->GetNumberField(TEXT("B"));
									Light.EmissiveColor.A = ColorObj->GetNumberField(TEXT("A"));
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
	Build();
}

FString ATrafficLightActor::ExportToJSON(bool bUseTransform) const
{
	TSharedPtr<FJsonObject> Root{MakeShared<FJsonObject>()};
	if (bUseTransform)
	{
		{
			FVector Loc = GetActorLocation();
			TSharedPtr<FJsonObject> JLoc = MakeShared<FJsonObject>();
			JLoc->SetNumberField(TEXT("X"), Loc.X);
			JLoc->SetNumberField(TEXT("Y"), Loc.Y);
			JLoc->SetNumberField(TEXT("Z"), Loc.Z);
			Root->SetObjectField(TEXT("WorldPosition"), JLoc);
		}
		{
			FRotator Rot = GetActorRotation();
			TSharedPtr<FJsonObject> JRot = MakeShared<FJsonObject>();
			JRot->SetNumberField(TEXT("X"), Rot.Pitch);
			JRot->SetNumberField(TEXT("Y"), Rot.Yaw);
			JRot->SetNumberField(TEXT("Z"), Rot.Roll);
			Root->SetObjectField(TEXT("WorldRotation"), JRot);
		}
		{
			FVector Scl = GetActorScale3D();
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
		TSharedPtr<FJsonObject> JP = MakeShared<FJsonObject>();

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

			TArray<TSharedPtr<FJsonValue>> JsonModules;
			for (const FTLModule& Module : Head.Modules)
			{
				TSharedPtr<FJsonObject> JM = MakeShared<FJsonObject>();
				if (Module.ModuleMesh)
				{
					JM->SetStringField(TEXT("ModuleMesh"), Module.ModuleMesh->GetName());
				}
				JM->SetObjectField(TEXT("Transform"), TransformToJson(Module.Transform));
				JM->SetObjectField(TEXT("Offset"), TransformToJson(Module.Offset));
				JM->SetBoolField(TEXT("bHasVisor"), Module.bHasVisor);

				// Lights
				TArray<TSharedPtr<FJsonValue>> JsonLights;
				for (const FTLModuleLight& L : Module.Lights)
				{
					TSharedPtr<FJsonObject> JL = MakeShared<FJsonObject>();
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

	FString Output;
	TSharedRef<TJsonWriter<>> Writer = TJsonWriterFactory<>::Create(&Output);
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

	HeadRoot->AttachToComponent(Parent, FAttachmentTransformRules::KeepRelativeTransform);
	HeadRoot->SetRelativeTransform(Head.Transform);

	return HeadRoot;
}

UStaticMeshComponent* ATrafficLightActor::AddModule(USceneComponent* Parent, FTLPole& Pole, FTLHead& Head, FTLModule& ModuleData)
{
	const FTransform ModuleTransform{ModuleData.Transform * ModuleData.Offset};

	UStaticMeshComponent* Comp{UMapGenFunctionLibrary::AddStaticMeshComponentToActor(this)};
	if (!Comp)
	{
		return nullptr;
	}
	Comp->SetStaticMesh(ModuleData.ModuleMesh);
	Comp->AttachToComponent(Parent, FAttachmentTransformRules::KeepRelativeTransform);
	Comp->SetRelativeTransform(ModuleTransform);

	int32 LightIndex{0};
	for (FTLModuleLight& Light : ModuleData.Lights)
	{
		const FName SlotName{*FString::Printf(TEXT("led_%d"), LightIndex)};
		const int32 MaterialIndex{Comp->GetMaterialIndex(SlotName)};
		if (MaterialIndex != INDEX_NONE)
		{
			UMaterialInstanceDynamic* MID{FMaterialFactory::GetLightMaterialInstance(Comp)};
			if (IsValid(MID))
			{
				MID->SetScalarParameterValue(TEXT("Emissive Intensity"), Light.EmissiveIntensity);
				MID->SetVectorParameterValue(TEXT("Emissive Color"), Light.EmissiveColor);
				MID->SetScalarParameterValue(TEXT("Offset U"), Light.U);
				MID->SetScalarParameterValue(TEXT("Offset V"), Light.V);
				Comp->SetMaterial(MaterialIndex, MID);
				Light.LightMID = MID;
				DemoLights.Add(&Light);
			}
			else
			{
				UE_LOG(LogCarlaDigitalTwinsTool, Warning, TEXT("Failed to create MID for slot %s"), *SlotName.ToString());
			}
			++LightIndex;
		}
	}

	Comp->Modify();
	ModuleMeshComponents.Add(Comp);
	ModuleData.Transform = ModuleTransform;
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
	if (IsValid(BaseComp))
	{
		const FBoxSphereBounds Bounds{BaseComp->GetStaticMesh()->GetBounds()};
		BaseLength = (Pole.Orientation == ETLOrientation::Horizontal) ? Bounds.BoxExtent.X * 2.0 : Bounds.BoxExtent.Z * 2.0;
	}

	double ExtLength{0.0};
	if (ExtLength > KINDA_SMALL_NUMBER)
	{
		const double Desired{FMath::Max(Pole.PoleHeight - BaseLength, 0.0)};
		const double ScaleRatio{Desired / ExtLength};
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
	UStaticMeshComponent* ExtensibleComp{Pole.BasePoleMeshComponent};
	if (!ExtensibleComp)
	{
		return nullptr;
	}

	UStaticMeshComponent* Comp{UMapGenFunctionLibrary::AddStaticMeshComponentToActor(this)};
	if (!Comp)
	{
		return nullptr;
	}

	Comp->SetStaticMesh(Pole.CapPoleMesh);
	static const FName CapSocketName(TEXT("cap"));
	Comp->AttachToComponent(ExtensibleComp, FAttachmentTransformRules::SnapToTargetIncludingScale, CapSocketName);
	Comp->Modify();

	Pole.CapPoleMeshComponent = Comp;
	return Comp;
}

void ATrafficLightActor::AddBackplate(USceneComponent* HeadRoot, FTLPole& Pole, FTLHead& Head)
{
	FBox LocalBounds{ForceInit};
	for (const FTLModule& Module : Head.Modules)
	{
		if (!Module.ModuleMeshComponent)
		{
			continue;
		}

		const FBox MeshBB{Module.ModuleMeshComponent->GetStaticMesh()->GetBoundingBox()};
		LocalBounds += MeshBB.TransformBy(Module.ModuleMeshComponent->GetRelativeTransform());
	}
	const FVector LMin{LocalBounds.Min};
	const FVector LMax{LocalBounds.Max};

	const double LocalHeight{LMax.Z - LMin.Z};
	const double ZScaleVert{LocalHeight / (FTLMeshFactory::GetBackplateVerticalMesh(Head)->GetBoundingBox().GetExtent().Z * 2.0)};

	const double LocalWidth{LMax.X - LMin.X};
	const double XScaleHorz{LocalWidth / (FTLMeshFactory::GetBackplateHorizontalMesh(Head)->GetBoundingBox().GetExtent().X * 2.0)};

	auto SpawnLocalPiece = [&](UStaticMesh* Mesh, const FVector& Loc, const FRotator& Rot, const FVector& Scale)
	{
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
		SpawnLocalPiece(FTLMeshFactory::GetBackplateCornerMesh(Head), P.Loc, P.Rot, FVector{1.0, 1.0, 1.0});
	}

	const TArray<FPlacement> Verticals{
		{{LMin.X, LMin.Y, LMin.Z}, {0.0, 0.0, 0.0}}, {{LMax.X, LMin.Y, LMin.Z + LocalHeight}, {180.0, 0.0, 0.0}}};
	for (const FPlacement& P : Verticals)
	{
		SpawnLocalPiece(FTLMeshFactory::GetBackplateVerticalMesh(Head), P.Loc, P.Rot, FVector{1.0, 1.0, ZScaleVert});
	}

	const TArray<FPlacement> Horizontals{
		{{LMin.X + LocalWidth, LMin.Y, LMax.Z}, {0.0, 0.0, 0.0}}, {{LMin.X, LMin.Y, LMin.Z}, {180.0, 0.0, 0.0}}};
	for (const FPlacement& P : Horizontals)
	{
		SpawnLocalPiece(FTLMeshFactory::GetBackplateHorizontalMesh(Head), P.Loc, P.Rot, FVector{XScaleHorz, 1.0, 1.0});
	}

	const FVector CenterLoc{LMin.X + (LMax.X - LMin.X), LMin.Y, LMin.Z};
	SpawnLocalPiece(
		FTLMeshFactory::GetBackplateMiddleMesh(Head), CenterLoc, FRotator{0.0, 0.0, 0.0}, FVector{XScaleHorz, 1.0, ZScaleVert});
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

		if (!IsValid(Prev.ModuleMeshComponent))
		{
			UE_LOG(LogCarlaDigitalTwinsTool, Warning, TEXT("Previous module mesh component is invalid at module %d"), i);
			continue;
		}
		if (!IsValid(Curr.ModuleMeshComponent))
		{
			UE_LOG(LogCarlaDigitalTwinsTool, Warning, TEXT("Missing component at module %d"), i);
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
		Curr.ModuleMeshComponent->SetRelativeTransform(Curr.Transform * Curr.Offset);
	}
}

void ATrafficLightActor::Clear()
{
	TArray<UActorComponent*> Old;
	GetComponents(Old);
	for (UActorComponent* C : Old)
	{
		if (C != RootComponent)
		{
			C->DestroyComponent();
			RemoveInstanceComponent(C);
		}
	}
	DemoLights.Empty();
}

void ATrafficLightActor::PlayDemoSequence()
{
	DemoLights.Empty();
	for (FTLPole& Pole : Poles)
	{
		for (FTLHead& Head : Pole.Heads)
		{
			for (FTLModule& Module : Head.Modules)
			{
				for (FTLModuleLight& Light : Module.Lights)
				{
					if (Light.LightMID)
					{
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
	GetWorldTimerManager().ClearTimer(PhaseTimerHandle);
	GetWorldTimerManager().ClearTimer(AmberBlinkTimerHandle);
}

void ATrafficLightActor::AdvanceDemoPhase()
{
	if (!bDemoPlaying)
	{
		return;
	}
	for (FTLModuleLight* LightPtr : DemoLights)
	{
		if (LightPtr && LightPtr->LightMID)
		{
			LightPtr->LightMID->SetScalarParameterValue(TEXT("Emissive Intensity"), 0.0f);
		}
	}

	switch (CurrentPhase)
	{
		case EDemoPhase::Red:
		{
			for (FTLModuleLight* Light : DemoLights)
			{
				if (Light->LightType == ETLLightType::SolidColorRed)
				{
					Light->LightMID->SetScalarParameterValue(TEXT("Emissive Intensity"), Light->EmissiveIntensity);
				}
				else if (Light->LightType == ETLLightType::PedestrianWalkGreen)
				{
					Light->LightMID->SetScalarParameterValue(TEXT("Emissive Intensity"), Light->EmissiveIntensity);
				}
			}
			CurrentPhase = EDemoPhase::Green;
			GetWorldTimerManager().ClearTimer(PhaseTimerHandle);
			GetWorldTimerManager().SetTimer(PhaseTimerHandle, this, &ATrafficLightActor::AdvanceDemoPhase, RedDuration, false);
			break;
		}
		case EDemoPhase::Green:
		{
			for (FTLModuleLight* Light : DemoLights)
			{
				if (Light->LightType == ETLLightType::SolidColorGreen)
				{
					Light->LightMID->SetScalarParameterValue(TEXT("Emissive Intensity"), Light->EmissiveIntensity);
				}
				else if (Light->LightType == ETLLightType::PedestrianStop)
				{
					Light->LightMID->SetScalarParameterValue(TEXT("Emissive Intensity"), Light->EmissiveIntensity);
				}
			}
			CurrentPhase = EDemoPhase::AmberBlink;
			bAmberVisible = false;
			GetWorldTimerManager().ClearTimer(PhaseTimerHandle);
			GetWorldTimerManager().SetTimer(PhaseTimerHandle, this, &ATrafficLightActor::AdvanceDemoPhase, GreenDuration, false);
			break;
		}
		case EDemoPhase::AmberBlink:
		{
			bAmberVisible = false;
			ToggleAmberBlink();
			GetWorldTimerManager().ClearTimer(AmberBlinkTimerHandle);
			GetWorldTimerManager().SetTimer(
				AmberBlinkTimerHandle, this, &ATrafficLightActor::ToggleAmberBlink, AmberBlinkInterval, true);
			GetWorldTimerManager().ClearTimer(PhaseTimerHandle);
			GetWorldTimerManager().SetTimer(PhaseTimerHandle, this, &ATrafficLightActor::EndAmberPhase, AmberBlinkDuration, false);

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
	bAmberVisible = !bAmberVisible;
	for (FTLModuleLight* Light : DemoLights)
	{
		if (Light->LightType == ETLLightType::SolidColorAmber)
		{
			Light->LightMID->SetScalarParameterValue(TEXT("Emissive Intensity"), bAmberVisible ? Light->EmissiveIntensity : 0.0f);
		}
		else if (Light->LightType == ETLLightType::PedestrianStop)
		{
			Light->LightMID->SetScalarParameterValue(TEXT("Emissive Intensity"), Light->EmissiveIntensity);
		}
	}
}

void ATrafficLightActor::EndAmberPhase()
{
	GetWorldTimerManager().ClearTimer(AmberBlinkTimerHandle);
	CurrentPhase = EDemoPhase::Red;
	AdvanceDemoPhase();
}
