// Copyright (c) 2017 Computer Vision Center (CVC) at the Universitat Autonoma de Barcelona (UAB). This work is licensed under the terms of the MIT license. For a copy, see <https://opensource.org/licenses/MIT>.

#undef CreateDirectory

#include "Online/CustomFileDownloader.h"
#include "Generation/OpenDriveToMap.h"
#include "Generation/OpenDriveFileGenerationParameters.h"
#include "HttpModule.h"
#include "Http.h"
#include "Misc/FileHelper.h"
#include "CarlaDigitalTwinsTool.h"
#include "Paths/GenerationPathsHelper.h"

#include <OSM2ODR.h>

void UCustomFileDownloader::ConvertOSMInOpenDrive(
    FString FilePath,
    float Lat_0, float Lon_0,
    const FOpenDriveFileGenerationParameters& OpenDriveGenParams)
{
  IPlatformFile &FileManager = FPlatformFileManager::Get().GetPlatformFile();

  FString FileContent;
  if (FileManager.FileExists(*FilePath))
  {
    if (!FFileHelper::LoadFileToString(FileContent, *FilePath, FFileHelper::EHashOptions::None))
    {
      UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("FileManipulation: Did not load text from \"%s\""), *FilePath);
      return;
    }
  }
  else
  {
    UE_LOG(LogCarlaDigitalTwinsTool, Warning, TEXT("File: \"%s\" does not exist"), *FilePath);
    return;
  }

  if (!FileContent.Contains(TEXT("<osm"), ESearchCase::IgnoreCase) ||
      !FileContent.Contains(TEXT("<node"), ESearchCase::IgnoreCase) ||
      !FileContent.Contains(TEXT("<way"), ESearchCase::IgnoreCase))
  {
    const FString Snippet = FileContent.Left(256).Replace(TEXT("\r"), TEXT(" ")).Replace(TEXT("\n"), TEXT(" "));
    UE_LOG(LogCarlaDigitalTwinsTool, Error,
           TEXT("FileManipulation: \"%s\" is not a valid OSM road payload (missing <osm>, <node>, or <way>). Snippet: %s"),
           *FilePath, *Snippet);
    return;
  }

  std::string OpenDriveFile;

  osm2odr::OSM2ODRSettings Settings;
  Settings.default_sidewalk_width = OpenDriveGenParams.DefaultSidewalkWidth;
  Settings.default_lane_width = OpenDriveGenParams.DefaultLaneWidth;
  Settings.elevation_layer_height = OpenDriveGenParams.DefaultOSMLayerHeight;
  const FString ProjCoords = FString::Printf(TEXT(" +lat_0=%.8f +lon_0=%.8f"), Lat_0, Lon_0);
  Settings.proj_string += TCHAR_TO_UTF8(*ProjCoords);
  Settings.center_map = false;

  try
  {
    FTCHARToUTF8 FileContentUTF8(*FileContent, FileContent.Len());
    auto OsmFile = std::string(FileContentUTF8.Get(), FileContentUTF8.Length());
    OpenDriveFile = osm2odr::ConvertOSMToOpenDRIVE(OsmFile, Settings);
  }
  catch (const std::exception& Ex)
  {
    const FString ErrorMsg = UTF8_TO_TCHAR(Ex.what());
    UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("FileManipulation: osm2odr::ConvertOSMToOpenDRIVE failed: %s"), *ErrorMsg);
    return;
  }
  catch (...)
  {
    UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("FileManipulation: osm2odr::ConvertOSMToOpenDRIVE failed with unknown exception."));
    return;
  }

  if (OpenDriveFile.empty())
  {
    UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("FileManipulation: osm2odr::ConvertOSMToOpenDRIVE produced empty output for \"%s\""), *FilePath);
    return;
  }

  FilePath.RemoveFromEnd(TEXT(".osm"), ESearchCase::Type::IgnoreCase);
  FilePath += TEXT(".xodr");

  FUTF8ToTCHAR OpenDriveTCHAR(OpenDriveFile.c_str(), OpenDriveFile.size());
  const FString OpenDriveString(OpenDriveTCHAR.Length(), OpenDriveTCHAR.Get());
  if (FFileHelper::SaveStringToFile(OpenDriveString, *FilePath, FFileHelper::EEncodingOptions::ForceUTF8WithoutBOM))
  {
    UE_LOG(LogCarlaDigitalTwinsTool, Display, TEXT("FileManipulation: Successfully Written: \"%s\" to the text file"), *FilePath);
  }
  else
  {
    UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("FileManipulation: Failed to write FString to file \"%s\"."), *FilePath);
  }
}

void UCustomFileDownloader::StartDownload()
{
  if (Url.IsEmpty() || ResultFileName.IsEmpty())
  {
    UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("StartDownload called with empty Url or ResultFileName"));
    return;
  }
  UE_LOG(LogCarlaDigitalTwinsTool, Log, TEXT("FHttpDownloader CREATED"));
  UE_LOG(LogCarlaDigitalTwinsTool, Log, TEXT("Map Name Is %s"), *ResultFileName );
  FHttpDownloader *Download = new FHttpDownloader("GET", Url, ResultFileName, DownloadDelegate);
  Download->Run();
}

FHttpDownloader::FHttpDownloader(const FString &InVerb, const FString &InUrl, const FString &InFilename, FDownloadComplete &Delegate)
    : Verb(InVerb), Url(InUrl), Filename(InFilename), DelegateToCall(Delegate)
{
}

FHttpDownloader::FHttpDownloader()
{

}

static TAutoConsoleVariable<float> CVarCFDTimeout(
	TEXT("CarlaDT.CustomFileDownloader.Timeout"),
	-1.0F,
	TEXT("Sets the timeout for OSM downloads."));

void FHttpDownloader::Run(void)
{
  UE_LOG(LogCarlaDigitalTwinsTool, Log, TEXT("Starting download [%s] Url=[%s]"), *Verb, *Url);
  TSharedPtr<IHttpRequest, ESPMode::ThreadSafe> Request = FHttpModule::Get().CreateRequest();
  UE_LOG(LogCarlaDigitalTwinsTool, Log, TEXT("Map Name Is %s"), *Filename );
  Request->OnProcessRequestComplete().BindRaw(this, &FHttpDownloader::RequestComplete);
  Request->SetURL(Url);
  const float Timeout = CVarCFDTimeout.GetValueOnAnyThread();
  if (Timeout > 0.0F)
  {
    Request->SetTimeout(Timeout);
#if ENGINE_MAJOR_VERSION >= 5
    Request->SetActivityTimeout(Timeout);
#endif
  }
  Request->SetVerb(Verb);
  if (!Request->ProcessRequest())
  {
    UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("Failed to start HTTP request [%s] Url=[%s]"), *Verb, *Url);
    delete this;
  }
}

void FHttpDownloader::RequestComplete(FHttpRequestPtr HttpRequest, FHttpResponsePtr HttpResponse, bool bSucceeded)
{
  if (!bSucceeded || !HttpResponse.IsValid())
  {
    UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("Download failed (bSucceeded=%d, validResponse=%d)"),
           bSucceeded ? 1 : 0, HttpResponse.IsValid() ? 1 : 0);
  }
  else
  {
    const int32 ResponseCode = HttpResponse->GetResponseCode();
    if (ResponseCode < 200 || ResponseCode >= 300)
    {
      UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("Error during download [%s] Url=[%s] Response=[%d]"),
             HttpRequest.IsValid() ? *HttpRequest->GetVerb() : TEXT(""),
             HttpRequest.IsValid() ? *HttpRequest->GetURL() : *Url,
             ResponseCode);
      delete this;
      return;
    }

    UE_LOG(LogCarlaDigitalTwinsTool, Log, TEXT("Completed download [%s] Url=[%s] Response=[%d]"),
           HttpRequest.IsValid() ? *HttpRequest->GetVerb() : TEXT(""),
           HttpRequest.IsValid() ? *HttpRequest->GetURL() : *Url,
           ResponseCode);

    const FString CurrentDir = FPaths::Combine(UGenerationPathsHelper::GetRawMapDirectoryPath(Filename), TEXT("OpenDrive"));
    UE_LOG(LogCarlaDigitalTwinsTool, Log, TEXT("FHttpDownloader::RequestComplete CurrentDir %s."), *CurrentDir);

    IPlatformFile &FileManager = FPlatformFileManager::Get().GetPlatformFile();
    if (!FileManager.DirectoryExists(*CurrentDir))
    {
      FileManager.CreateDirectoryTree(*CurrentDir);
    }
    const FString CurrentFile = FPaths::Combine(CurrentDir, Filename + TEXT(".osm"));

    const FString StringToWrite = HttpResponse->GetContentAsString();
    if (!StringToWrite.Contains(TEXT("<osm"), ESearchCase::IgnoreCase) ||
        !StringToWrite.Contains(TEXT("<node"), ESearchCase::IgnoreCase) ||
        !StringToWrite.Contains(TEXT("<way"), ESearchCase::IgnoreCase))
    {
      const FString Snippet = StringToWrite.Left(256).Replace(TEXT("\r"), TEXT(" ")).Replace(TEXT("\n"), TEXT(" "));
      UE_LOG(LogCarlaDigitalTwinsTool, Error,
             TEXT("Download response for \"%s\" does not contain valid OSM road data (<osm>/<node>/<way>). Snippet: %s"),
             *Filename, *Snippet);
      delete this;
      return;
    }

    if (FFileHelper::SaveStringToFile(StringToWrite, *CurrentFile, FFileHelper::EEncodingOptions::ForceUTF8WithoutBOM))
    {
      UE_LOG(LogCarlaDigitalTwinsTool, Log, TEXT("FileManipulation: Successfully Written \"%s\""), *CurrentFile);
      DelegateToCall.ExecuteIfBound();
    }
    else
    {
      UE_LOG(LogCarlaDigitalTwinsTool, Error, TEXT("FileManipulation: Failed to write FString to file \"%s\"."), *CurrentFile);
    }
  }
  delete this;
}
